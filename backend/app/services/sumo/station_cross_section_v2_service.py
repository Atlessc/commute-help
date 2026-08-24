"""Versioned station projection over comparison-relations-v2 static geometry."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import sumolib
from pyproj import Transformer
from shapely.geometry import LineString

from backend.app.schemas.sumo_comparison_relations import ComparisonRelationManifestV2
from backend.app.schemas.sumo_station_cross_sections import (
    E1_FEASIBILITY_VERSION,
    STATION_CROSS_SECTION_ALGORITHM_VERSION,
    STATION_CROSS_SECTION_GEOMETRY_SOURCE_V2,
    STATION_CROSS_SECTION_PRODUCER_VERSION_V2,
    STATION_CROSS_SECTION_REPAIR_REASON_V2,
    STATION_CROSS_SECTION_SCHEMA_VERSION_V2,
    StationCrossSectionManifestV1,
    StationCrossSectionManifestV2,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.station_cross_section_service import (
    PROJECTED_CRS,
    WGS84,
    _canonical_portal_station_id,
    project_station_to_relation,
)

CROSS_SECTION_DIRECTORY_V2 = "station-cross-sections-v2"
CROSS_SECTION_PARQUET = "station-sumo-cross-sections.parquet"
CROSS_SECTION_SUMMARY = "station-cross-section-summary.json"
CROSS_SECTION_REPORT = "STATION_CROSS_SECTION_ANALYSIS.md"
CROSS_SECTION_MANIFEST = "station-cross-section-manifest.json"
E1_FEASIBILITY_REPORT = "e1-feasibility.json"


class StationCrossSectionV2Error(RuntimeError):
    """The immutable V2 station projection cannot satisfy its static contract."""


def build_station_cross_sections_v2(
    *,
    parent_directory: Path,
    relation_directory: Path,
    station_mapping_path: Path,
    detector_metadata_path: Path,
    edge_map_path: Path,
    graph_edges_path: Path,
    graph_manifest_path: Path,
    sumo_network_path: Path,
    sumo_network_manifest_path: Path,
    comparison_validation_directory: Path,
    output_directory: Path,
) -> StationCrossSectionManifestV2:
    """Reproject the frozen station domain using V2 relations and frozen SUMO shapes."""
    if output_directory.exists():
        raise StationCrossSectionV2Error(
            f"immutable station-cross-sections-v2 already exists: {output_directory}"
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        raise StationCrossSectionV2Error(
            f"preserved pending station-cross-sections-v2 exists: {pending}"
        )

    parent_manifest_path = parent_directory / CROSS_SECTION_MANIFEST
    parent_manifest = StationCrossSectionManifestV1.model_validate_json(
        parent_manifest_path.read_text(encoding="utf-8")
    )
    parent_mapping_path = parent_directory / parent_manifest.mapping_output.relative_path
    parent_e1_path = parent_directory / parent_manifest.e1_feasibility_output.relative_path
    _require_sha(parent_mapping_path, parent_manifest.mapping_output.sha256)
    _require_sha(parent_e1_path, parent_manifest.e1_feasibility_output.sha256)
    _require_sha(station_mapping_path, parent_manifest.station_mapping_sha256)
    _require_sha(detector_metadata_path, parent_manifest.detector_metadata_sha256)
    _require_sha(edge_map_path, parent_manifest.edge_map_sha256)

    relation_manifest_path = relation_directory / "comparison-relation-manifest.json"
    relation_manifest = ComparisonRelationManifestV2.model_validate_json(
        relation_manifest_path.read_text(encoding="utf-8")
    )
    relation_parquet_path = (
        relation_directory / relation_manifest.relation_output.relative_path
    )
    _require_sha(relation_parquet_path, relation_manifest.relation_output.sha256)

    graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    graph_edges_sha = _sha256(graph_edges_path)
    if graph_manifest["artifacts"]["edges_parquet"]["sha256"] != graph_edges_sha:
        raise StationCrossSectionV2Error("APP graph edges differ from graph manifest")
    if relation_manifest.graph_edges_sha256 != graph_edges_sha:
        raise StationCrossSectionV2Error("APP graph edges differ from comparison V2")
    if relation_manifest.graph_version != graph_manifest["graph_version"]:
        raise StationCrossSectionV2Error("APP graph version differs from comparison V2")

    network_manifest = json.loads(
        sumo_network_manifest_path.read_text(encoding="utf-8")
    )
    sumo_network_sha = _sha256(sumo_network_path)
    if network_manifest["artifact"]["sha256"] != sumo_network_sha:
        raise StationCrossSectionV2Error("SUMO network differs from network manifest")
    if relation_manifest.sumo_network_sha256 != sumo_network_sha:
        raise StationCrossSectionV2Error("SUMO network differs from comparison V2")
    if relation_manifest.sumo_network_version != network_manifest["network_version"]:
        raise StationCrossSectionV2Error("SUMO network version differs from comparison V2")

    comparison_summary_path = comparison_validation_directory / "summary.json"
    comparison_provenance_path = comparison_validation_directory / "provenance.json"
    comparison_summary = json.loads(comparison_summary_path.read_text(encoding="utf-8"))
    comparison_provenance = json.loads(
        comparison_provenance_path.read_text(encoding="utf-8")
    )
    if comparison_summary.get("status") != "PASS_TRAFFIC_BLIND_STATIC_VALIDATION":
        raise StationCrossSectionV2Error("comparison V2 validation did not pass")
    guards = comparison_summary.get("scientific_guards", {})
    if any(
        bool(guards.get(key))
        for key in (
            "traffic_values_loaded",
            "development_loaded",
            "blind_loaded",
            "sumo_behavior_executed",
            "methodology_modified",
            "builder_relaxed",
            "clustering_case_repaired",
        )
    ):
        raise StationCrossSectionV2Error("comparison V2 scientific guard failed")
    if comparison_provenance.get("candidate_content_digest") != relation_manifest.content_digest:
        raise StationCrossSectionV2Error("comparison V2 validation digest mismatch")

    station_map = pd.read_csv(
        station_mapping_path,
        dtype={"longitude": "string", "latitude": "string"},
    )
    accepted_stations = station_map.loc[station_map["status"].eq("accepted")].copy()
    accepted_stations["station_id"] = accepted_stations["station_id"].astype(str)
    accepted_stations["edge_id"] = accepted_stations["edge_id"].astype(str)
    if not accepted_stations["station_id"].is_unique:
        raise StationCrossSectionV2Error("accepted station identities are not unique")

    parent = pq.read_table(parent_mapping_path).to_pandas()
    parent["station_id"] = parent["station_id"].astype(str)
    parent["app_edge_id"] = parent["app_edge_id"].astype(str)
    if not parent["station_id"].is_unique:
        raise StationCrossSectionV2Error("parent station identities are not unique")
    if set(parent["station_id"]) != set(accepted_stations["station_id"]):
        raise StationCrossSectionV2Error("station domain differs from frozen V1 parent")
    parent_by_station = parent.set_index("station_id").to_dict(orient="index")
    _require_station_source_equality(accepted_stations, parent_by_station)

    detector_rows = json.loads(detector_metadata_path.read_text(encoding="utf-8"))
    detector_by_station: dict[str, list[str]] = {}
    for detector in detector_rows:
        station_id = _canonical_portal_station_id(detector["stationid"])
        detector_by_station.setdefault(station_id, []).append(
            f"portal-detector-{detector['detectorid']}"
        )

    relations = pq.read_table(relation_parquet_path).to_pandas()
    relations["app_edge_id"] = relations["app_edge_id"].astype(str)
    if not relations["app_edge_id"].is_unique:
        raise StationCrossSectionV2Error("comparison V2 APP edge identities are not unique")
    changed_relations = relations.loc[relations["boundary_repair_applied"].eq(True)]
    changed_app_edge_ids = sorted(changed_relations["app_edge_id"].astype(str))
    if len(changed_app_edge_ids) != relation_manifest.changed_relation_count:
        raise StationCrossSectionV2Error("comparison V2 changed-relation count mismatch")
    diagnostic_changed = sorted(
        str(row["app_edge_id"])
        for row in comparison_summary.get("changed_relations", [])
    )
    if changed_app_edge_ids != diagnostic_changed:
        raise StationCrossSectionV2Error("comparison V2 changed APP edge set mismatch")
    relation_lookup = relations.set_index("app_edge_id").to_dict(orient="index")
    dependency_station_ids = sorted(
        accepted_stations.loc[
            accepted_stations["edge_id"].isin(changed_app_edge_ids), "station_id"
        ].astype(str)
    )
    diagnostic_dependency_station_ids = sorted(
        str(station_id)
        for impact in comparison_summary.get("downstream_dependency_impacts", [])
        for station_id in impact.get("station_ids", [])
    )
    if dependency_station_ids != diagnostic_dependency_station_ids:
        raise StationCrossSectionV2Error("comparison V2 dependency station set mismatch")

    station_app_edge_ids = set(accepted_stations["edge_id"])
    relevant_relations = relations.loc[
        relations["app_edge_id"].isin(station_app_edge_ids)
    ]
    member_ids = sorted(
        {
            edge_id
            for value in relevant_relations["ordered_sumo_edge_ids_json"]
            for edge_id in _parse_json_strings(value)
        }
    )
    member_geometries, member_lane_ids = _load_sumo_member_geometry(
        sumo_network_path, member_ids
    )
    member_lengths = _relation_member_lengths(relevant_relations)

    app_station_counts = (
        accepted_stations.groupby("edge_id")["station_id"].nunique().to_dict()
    )
    rows: list[dict[str, Any]] = []
    for station in accepted_stations.sort_values(
        "station_id", kind="mergesort"
    ).itertuples(index=False):
        station_id = str(station.station_id)
        app_edge_id = str(station.edge_id)
        parent_row = parent_by_station[station_id]
        station_longitude = float(parent_row["station_longitude"])
        station_latitude = float(parent_row["station_latitude"])
        relation = relation_lookup.get(app_edge_id)
        if relation is None:
            projection = _empty_projection()
            relation = {"relation_id": None, "relation_class": "review_or_absent"}
        else:
            projection = project_station_to_relation(
                station_id=station_id,
                longitude=station_longitude,
                latitude=station_latitude,
                direction=str(station.direction),
                relation=relation,
                member_geometries=member_geometries,
                member_lengths_m=member_lengths,
            )
        sumo_edge_id = projection.get("sumo_edge_id")
        lane_ids = member_lane_ids.get(str(sumo_edge_id), []) if sumo_edge_id else []
        mapping_id = hashlib.sha256(
            canonical_json(
                {
                    "station_id": station_id,
                    "app_edge_id": app_edge_id,
                    "relation_id": relation.get("relation_id"),
                    "projection_algorithm": STATION_CROSS_SECTION_ALGORITHM_VERSION,
                }
            ).encode()
        ).hexdigest()
        rows.append(
            {
                "schema_version": STATION_CROSS_SECTION_SCHEMA_VERSION_V2,
                "mapping_id": mapping_id,
                "station_id": station_id,
                "app_edge_id": app_edge_id,
                "relation_id": relation.get("relation_id"),
                "relation_class": relation.get("relation_class"),
                "sumo_edge_id": sumo_edge_id,
                "chain_member_index": projection.get("chain_member_index"),
                "projected_position_m": projection.get("projected_position_m"),
                "normalized_position": projection.get("normalized_position"),
                "chain_position_m": projection.get("chain_position_m"),
                "projection_distance_m": projection.get("projection_distance_m"),
                "member_length_m": projection.get("member_length_m"),
                "distance_to_upstream_member_boundary_m": projection.get(
                    "distance_to_upstream_member_boundary_m"
                ),
                "distance_to_downstream_member_boundary_m": projection.get(
                    "distance_to_downstream_member_boundary_m"
                ),
                "nearest_boundary_distance_m": projection.get(
                    "nearest_boundary_distance_m"
                ),
                "effectively_equal_member_count": projection.get(
                    "effectively_equal_member_count"
                ),
                "candidate_member_count": projection.get("candidate_member_count"),
                "direction": str(station.direction),
                "direction_compatible": projection.get("direction_compatible"),
                "projection_status": projection["projection_status"],
                "projection_reason": projection["projection_reason"],
                "station_longitude": station_longitude,
                "station_latitude": station_latitude,
                "highway_name": str(station.highway_name),
                "location_text": str(station.location_text),
                "milepost": _optional_number(station.milepost),
                "station_app_mapping_score": _optional_number(station.score),
                "station_app_mapping_confidence": str(station.confidence),
                "station_app_mapping_distance_m": _optional_number(station.distance_m),
                "source_detector_ids_json": canonical_json(
                    sorted(detector_by_station.get(station_id, []))
                ),
                "source_detector_count": len(detector_by_station.get(station_id, [])),
                "historical_profile_participation_count": int(
                    parent_row["historical_profile_participation_count"]
                ),
                "stations_on_app_edge": int(app_station_counts.get(app_edge_id, 0)),
                "sumo_lane_ids_json": canonical_json(sorted(lane_ids)),
                "sumo_lane_count": len(lane_ids),
                "virtual_station_group_id": f"portal-sumo-station::{station_id}",
                "projection_algorithm_version": STATION_CROSS_SECTION_ALGORITHM_VERSION,
            }
        )

    frame = pd.DataFrame(rows).sort_values(
        "station_id", kind="mergesort"
    ).reset_index(drop=True)
    summary = _build_summary(
        frame,
        changed_app_edge_ids=changed_app_edge_ids,
        dependency_station_ids=dependency_station_ids,
    )
    report = _render_report(summary)

    pending.mkdir(parents=True)
    try:
        mapping_path = pending / CROSS_SECTION_PARQUET
        summary_path = pending / CROSS_SECTION_SUMMARY
        report_path = pending / CROSS_SECTION_REPORT
        feasibility_path = pending / E1_FEASIBILITY_REPORT
        manifest_path = pending / CROSS_SECTION_MANIFEST
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
            {
                b"schema_version": b"2",
                b"producer": b"portal_station_sumo_cross_section_projector_v2",
                b"producer_version": STATION_CROSS_SECTION_PRODUCER_VERSION_V2.encode(),
                b"projection_algorithm": STATION_CROSS_SECTION_ALGORITHM_VERSION.encode(),
                b"geometry_source": STATION_CROSS_SECTION_GEOMETRY_SOURCE_V2.encode(),
                b"acceptance_policy": b"none_characterization_only",
            }
        )
        pq.write_table(table, mapping_path, compression="zstd", compression_level=9)
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        shutil.copyfile(parent_e1_path, feasibility_path)
        payload: dict[str, Any] = {
            "schema_version": STATION_CROSS_SECTION_SCHEMA_VERSION_V2,
            "artifact_type": "commute_help_station_sumo_cross_section_analysis",
            "artifact_status": "complete_characterization_pending_acceptance_policy",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC).isoformat(),
            "producer_name": "portal_station_sumo_cross_section_projector_v2",
            "producer_version": STATION_CROSS_SECTION_PRODUCER_VERSION_V2,
            "projection_algorithm_version": STATION_CROSS_SECTION_ALGORITHM_VERSION,
            "geometry_source": STATION_CROSS_SECTION_GEOMETRY_SOURCE_V2,
            "projected_crs": PROJECTED_CRS,
            "source_crs": WGS84,
            "coordinate_representation_contract": "inherit_frozen_station_cross_sections_v1_binary64_coordinates",
            "coordinate_provenance_classification": "C_RUNTIME_LIBRARY_VERSION_DEPENDENT_REPRESENTATION",
            "source_coordinate_identity_guard": "frozen_station_mapping_sha256_plus_parent_v1_station_domain_binding_direction_and_finite_decimal_tokens",
            "repair_reason": STATION_CROSS_SECTION_REPAIR_REASON_V2,
            "e1_feasibility_version": E1_FEASIBILITY_VERSION,
            "graph_version": relation_manifest.graph_version,
            "sumo_network_version": relation_manifest.sumo_network_version,
            "parent_manifest_sha256": _sha256(parent_manifest_path),
            "parent_content_digest": parent_manifest.content_digest,
            "parent_mapping_sha256": parent_manifest.mapping_output.sha256,
            "parent_row_content_sha256": parent_manifest.row_content_sha256,
            "station_mapping_sha256": _sha256(station_mapping_path),
            "detector_metadata_sha256": _sha256(detector_metadata_path),
            "edge_map_sha256": _sha256(edge_map_path),
            "graph_edges_sha256": graph_edges_sha,
            "graph_manifest_sha256": _sha256(graph_manifest_path),
            "sumo_network_sha256": sumo_network_sha,
            "sumo_network_manifest_sha256": _sha256(sumo_network_manifest_path),
            "comparison_relation_manifest_sha256": _sha256(relation_manifest_path),
            "comparison_relation_content_digest": relation_manifest.content_digest,
            "comparison_relation_parquet_sha256": relation_manifest.relation_output.sha256,
            "comparison_validation_summary_sha256": _sha256(comparison_summary_path),
            "comparison_validation_provenance_sha256": _sha256(
                comparison_provenance_path
            ),
            "historical_profile_content_digest": parent_manifest.historical_profile_content_digest,
            "quality_policy_content_digest": parent_manifest.quality_policy_content_digest,
            "parent_e1_feasibility_sha256": parent_manifest.e1_feasibility_output.sha256,
            "changed_app_edge_ids": changed_app_edge_ids,
            "dependency_station_ids": dependency_station_ids,
            "scientific_guards": summary["scientific_guards"],
            "acceptance_policy": "none_characterize_distance_and_boundary_distributions_before_threshold_selection",
            "mapping_output": _file_identity(mapping_path, len(frame)),
            "summary_output": _file_identity(summary_path),
            "report_output": _file_identity(report_path),
            "e1_feasibility_output": _file_identity(feasibility_path),
            "row_content_sha256": _frame_digest(frame),
        }
        payload["content_digest"] = _content_digest(payload)
        manifest = StationCrossSectionManifestV2.model_validate(payload)
        manifest_path.write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )
        _promote(pending, output_directory)
        return manifest
    except BaseException:
        # Preserve pending evidence for fail-closed runtime diagnosis.
        raise


def _load_sumo_member_geometry(
    sumo_network_path: Path, member_ids: list[str]
) -> tuple[dict[str, LineString], dict[str, list[str]]]:
    network = sumolib.net.readNet(str(sumo_network_path), withInternal=False)
    transformer = Transformer.from_crs(WGS84, PROJECTED_CRS, always_xy=True)
    geometries: dict[str, LineString] = {}
    lane_ids: dict[str, list[str]] = {}
    for edge_id in member_ids:
        try:
            edge = network.getEdge(edge_id)
        except KeyError as exc:
            raise StationCrossSectionV2Error(
                f"comparison relation member is absent from frozen SUMO network: {edge_id}"
            ) from exc
        if edge.getFunction() == "internal":
            raise StationCrossSectionV2Error(
                f"internal SUMO edge entered comparison relation: {edge_id}"
            )
        shape = edge.getShape()
        if len(shape) < 2:
            raise StationCrossSectionV2Error(
                f"SUMO relation member has unusable geometry: {edge_id}"
            )
        geographic = [network.convertXY2LonLat(x, y) for x, y in shape]
        projected = [transformer.transform(lon, lat) for lon, lat in geographic]
        geometry = LineString(projected)
        if geometry.is_empty or not math.isfinite(float(geometry.length)) or geometry.length <= 0:
            raise StationCrossSectionV2Error(
                f"SUMO relation member has invalid projected geometry: {edge_id}"
            )
        geometries[edge_id] = geometry
        lane_ids[edge_id] = sorted(lane.getID() for lane in edge.getLanes())
    return geometries, lane_ids


def _relation_member_lengths(relations: pd.DataFrame) -> dict[str, float]:
    lengths: dict[str, float] = {}
    for value in relations["member_topology_evidence_json"]:
        for evidence in json.loads(str(value)):
            edge_id = str(evidence["sumo_edge_id"])
            length = evidence.get("length_m")
            if length is None or not math.isfinite(float(length)) or float(length) <= 0:
                raise StationCrossSectionV2Error(
                    f"relation member has invalid static length: {edge_id}"
                )
            numeric = float(length)
            previous = lengths.get(edge_id)
            if previous is not None and previous != numeric:
                raise StationCrossSectionV2Error(
                    f"relation member has inconsistent static lengths: {edge_id}"
                )
            lengths[edge_id] = numeric
    return lengths


def _require_station_source_equality(
    accepted_stations: pd.DataFrame, parent_by_station: dict[str, dict[str, Any]]
) -> None:
    for station in accepted_stations.itertuples(index=False):
        station_id = str(station.station_id)
        parent = parent_by_station[station_id]
        checks = {
            "app_edge_id": str(station.edge_id) == str(parent["app_edge_id"]),
            "direction": str(station.direction) == str(parent["direction"]),
            "parent_longitude_finite": math.isfinite(
                float(parent["station_longitude"])
            ),
            "parent_latitude_finite": math.isfinite(float(parent["station_latitude"])),
            "source_longitude_decimal": _is_finite_decimal_token(station.longitude),
            "source_latitude_decimal": _is_finite_decimal_token(station.latitude),
        }
        if not all(checks.values()):
            failed = sorted(key for key, passed in checks.items() if not passed)
            raise StationCrossSectionV2Error(
                f"frozen station source differs from V1 parent for {station_id}: {failed}"
            )


def _is_finite_decimal_token(value: Any) -> bool:
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _build_summary(
    frame: pd.DataFrame,
    *,
    changed_app_edge_ids: list[str],
    dependency_station_ids: list[str],
) -> dict[str, Any]:
    return {
        "artifact_status": "complete_characterization_pending_acceptance_policy",
        "calibration_status": "not_calibrated",
        "station_count": len(frame),
        "changed_app_edge_ids": changed_app_edge_ids,
        "dependency_station_ids": dependency_station_ids,
        "projection_status_counts": {
            str(key): int(value)
            for key, value in frame["projection_status"].value_counts().sort_index().items()
        },
        "relation_class_counts": {
            str(key): int(value)
            for key, value in frame["relation_class"].value_counts().sort_index().items()
        },
        "selected_sumo_member_counts": {
            str(key): int(value)
            for key, value in frame["sumo_edge_id"].dropna().value_counts().sort_index().items()
        },
        "projection_distance_m": _distribution(frame["projection_distance_m"]),
        "nearest_boundary_distance_m": _distribution(
            frame["nearest_boundary_distance_m"]
        ),
        "scientific_guards": {
            "traffic_values_loaded": False,
            "sumo_behavior_executed": False,
            "development_loaded": False,
            "blind_loaded": False,
            "projection_methodology_modified": False,
            "station_eligibility_modified": False,
        },
    }


def _render_report(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Station cross-sections V2 static regeneration",
            "",
            "Station projections inherited the frozen V1 binary64 station coordinates and were regenerated over comparison-relations-v2 using frozen SUMO network geometry.",
            "",
            f"- Station rows: {summary['station_count']:,}",
            f"- Changed comparison APP edges: {len(summary['changed_app_edge_ids'])}",
            f"- Direct dependency stations: {len(summary['dependency_station_ids'])}",
            "- Projection algorithm: relation-member-projection-v1 (unchanged)",
            "- Coordinate representation: inherited binary64-exactly from frozen station-cross-sections-v1",
            "- SUMO behavior executed: no",
            "- Traffic values loaded: no",
            "",
            "Acceptance remains pending the existing frozen policy lineage and its future V2 regeneration.",
            "",
        ]
    )


def _empty_projection() -> dict[str, Any]:
    return {
        "sumo_edge_id": None,
        "chain_member_index": None,
        "projected_position_m": None,
        "normalized_position": None,
        "chain_position_m": None,
        "projection_distance_m": None,
        "distance_to_upstream_member_boundary_m": None,
        "distance_to_downstream_member_boundary_m": None,
        "nearest_boundary_distance_m": None,
        "member_length_m": None,
        "direction_compatible": False,
        "effectively_equal_member_count": 0,
        "candidate_member_count": 0,
        "projection_status": "review_topology",
        "projection_reason": "accepted_app_edge_has_no_accepted_sumo_relation",
    }


def _parse_json_strings(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise StationCrossSectionV2Error("expected a JSON list")
    return [str(item) for item in parsed]


def _optional_number(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def _distribution(series: pd.Series) -> dict[str, float | int | None]:
    values = series.dropna().astype(float)
    if values.empty:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": float(values.min()),
        "median": float(values.quantile(0.5, interpolation="linear")),
        "max": float(values.max()),
    }


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        digest.update(
            canonical_json(
                {
                    key: None
                    if pd.isna(value)
                    else value.item()
                    if hasattr(value, "item")
                    else value
                    for key, value in row.items()
                }
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise StationCrossSectionV2Error(
            f"SHA-256 mismatch for {path}: expected {expected}, found {actual}"
        )


def _file_identity(path: Path, rows: int | None = None) -> dict[str, Any]:
    return {
        "relative_path": path.name,
        "sha256": _sha256(path),
        "byte_count": path.stat().st_size,
        "row_count": rows,
    }


def _content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _promote(pending: Path, output: Path) -> None:
    manifest = pending / CROSS_SECTION_MANIFEST
    if output.exists() or not manifest.is_file():
        raise StationCrossSectionV2Error("partial V2 cross-section artifact cannot promote")
    StationCrossSectionManifestV2.model_validate_json(
        manifest.read_text(encoding="utf-8")
    )
    os.replace(pending, output)
