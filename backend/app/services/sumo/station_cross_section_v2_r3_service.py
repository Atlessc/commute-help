"""Build dependency-local station-cross-sections-v2-r3 without numerical churn."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import wkb
from shapely.geometry import LineString

from backend.app.schemas.sumo_station_cross_sections import StationCrossSectionManifestV1
from backend.app.schemas.sumo_station_cross_sections_v2_r3 import (
    StationCrossSectionManifestV2R3,
    SyntheticBoundaryGeometryManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.station_cross_section_service import (
    STATION_CROSS_SECTION_ALGORITHM_VERSION,
    project_station_to_relation,
)

EXPECTED_CHANGED_APP_EDGES = {
    "58982054bb2b4202e8b09b04",
    "6bbb95754afd8b91412741d2",
    "a5c8af67423f92712af0a5ab",
}
EXPECTED_CHANGED_STATIONS = {
    "portal-station-1013",
    "portal-station-1060",
    "portal-station-3117",
    "portal-station-3118",
    "portal-station-3132",
    "portal-station-3194",
}
MAPPING = "station-sumo-cross-sections.parquet"
ROW_PROVENANCE = "station-row-provenance.parquet"
SUMMARY = "station-cross-section-summary.json"
REPORT = "STATION_CROSS_SECTION_ANALYSIS.md"
MANIFEST = "station-cross-section-manifest.json"
E1 = "e1-feasibility.json"


class StationCrossSectionV2R3Error(RuntimeError):
    """The dependency-local V2-R3 candidate violates frozen lineage."""


def build_station_cross_sections_v2_r3(
    *,
    parent_directory: Path,
    rejected_candidate_directory: Path,
    comparison_v1_directory: Path,
    comparison_v2_directory: Path,
    comparison_validation_directory: Path,
    station_mapping_path: Path,
    edge_map_path: Path,
    synthetic_geometry_directory: Path,
    synthetic_geometry_validation_summary_path: Path,
    abc_diagnostic_directory: Path,
    output_directory: Path,
) -> StationCrossSectionManifestV2R3:
    if output_directory.exists():
        raise StationCrossSectionV2R3Error(f"immutable output exists: {output_directory}")
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        raise StationCrossSectionV2R3Error(f"preserved pending output exists: {pending}")

    parent_manifest_path = parent_directory / MANIFEST
    parent_mapping_path = parent_directory / MAPPING
    parent_e1_path = parent_directory / E1
    rejected_manifest_path = rejected_candidate_directory / MANIFEST
    rejected_mapping_path = rejected_candidate_directory / MAPPING
    comparison_v1_manifest_path = comparison_v1_directory / "comparison-relation-manifest.json"
    comparison_v1_parquet_path = comparison_v1_directory / "app-sumo-comparison-relations.parquet"
    comparison_v2_manifest_path = comparison_v2_directory / "comparison-relation-manifest.json"
    comparison_v2_parquet_path = comparison_v2_directory / "app-sumo-comparison-relations.parquet"
    comparison_validation_summary_path = comparison_validation_directory / "summary.json"
    geometry_manifest_path = synthetic_geometry_directory / "manifest.json"
    geometry_parquet_path = synthetic_geometry_directory / "synthetic-boundary-member-geometries.parquet"
    geometry_validation_provenance_path = (
        synthetic_geometry_validation_summary_path.parent / "provenance.json"
    )
    abc_summary_path = abc_diagnostic_directory / "summary.json"
    abc_provenance_path = abc_diagnostic_directory / "provenance.json"

    parent_manifest = StationCrossSectionManifestV1.model_validate_json(
        parent_manifest_path.read_text(encoding="utf-8")
    )
    rejected_manifest = json.loads(rejected_manifest_path.read_text(encoding="utf-8"))
    comparison_v1_manifest = json.loads(comparison_v1_manifest_path.read_text(encoding="utf-8"))
    comparison_v2_manifest = json.loads(comparison_v2_manifest_path.read_text(encoding="utf-8"))
    comparison_validation = json.loads(
        comparison_validation_summary_path.read_text(encoding="utf-8")
    )
    geometry_manifest = SyntheticBoundaryGeometryManifestV1.model_validate_json(
        geometry_manifest_path.read_text(encoding="utf-8")
    )
    geometry_validation = json.loads(
        synthetic_geometry_validation_summary_path.read_text(encoding="utf-8")
    )
    abc_summary = json.loads(abc_summary_path.read_text(encoding="utf-8"))

    require_sha(parent_mapping_path, parent_manifest.mapping_output.sha256)
    require_sha(parent_e1_path, parent_manifest.e1_feasibility_output.sha256)
    require_sha(station_mapping_path, parent_manifest.station_mapping_sha256)
    require_sha(edge_map_path, parent_manifest.edge_map_sha256)
    require_sha(rejected_mapping_path, str(rejected_manifest["mapping_output"]["sha256"]))
    require_sha(comparison_v1_parquet_path, str(comparison_v1_manifest["relation_output"]["sha256"]))
    require_sha(comparison_v2_parquet_path, str(comparison_v2_manifest["relation_output"]["sha256"]))
    require_sha(geometry_parquet_path, geometry_manifest.geometry_output.sha256)
    if comparison_validation.get("status") != "PASS_TRAFFIC_BLIND_STATIC_VALIDATION":
        raise StationCrossSectionV2R3Error("comparison-v2 validation did not pass")
    if geometry_validation.get("status") != "PASS_SYNTHETIC_BOUNDARY_GEOMETRY_STATIC_VALIDATION":
        raise StationCrossSectionV2R3Error("synthetic geometry validation did not pass")
    if geometry_validation.get("manifest_sha256") != sha256_file(geometry_manifest_path):
        raise StationCrossSectionV2R3Error("synthetic geometry validation manifest differs")
    if geometry_validation.get("parquet_sha256") != sha256_file(geometry_parquet_path):
        raise StationCrossSectionV2R3Error("synthetic geometry validation Parquet differs")
    if geometry_validation.get("sumo_network_sha256") != geometry_manifest.sumo_network_sha256:
        raise StationCrossSectionV2R3Error("synthetic geometry validation network differs")
    if abc_summary.get("classification") != "D_MIXED_RUNTIME_AND_IMPLEMENTATION_EFFECT":
        raise StationCrossSectionV2R3Error("A/B/C repair classification differs")
    if abc_summary.get("candidate_acceptance") != "REJECTED_VALIDATION_FAILED_CLOSED":
        raise StationCrossSectionV2R3Error("rejected-candidate disposition differs")

    parent = read_station_frame(parent_mapping_path)
    rejected = read_station_frame(rejected_mapping_path)
    relation_v1 = read_relation_frame(comparison_v1_parquet_path)
    relation_v2 = read_relation_frame(comparison_v2_parquet_path)
    relation_v1_by_app = relation_v1.set_index("app_edge_id", drop=False)
    relation_v2_by_app = relation_v2.set_index("app_edge_id", drop=False)
    changed_app_edges = set(
        relation_v2.loc[relation_v2["boundary_repair_applied"].eq(True), "app_edge_id"]
    )
    if changed_app_edges != EXPECTED_CHANGED_APP_EDGES:
        raise StationCrossSectionV2R3Error(
            f"changed APP-edge set differs: {sorted(changed_app_edges)}"
        )

    source = pd.read_csv(
        station_mapping_path,
        dtype={"longitude": "string", "latitude": "string"},
    )
    source = source.loc[source["status"].eq("accepted")].copy()
    source["station_id"] = source["station_id"].astype(str)
    source["edge_id"] = source["edge_id"].astype(str)
    source_by_id = source.set_index("station_id", drop=False)
    parent_by_id = parent.set_index("station_id", drop=False)
    if set(source_by_id.index) != set(parent_by_id.index):
        raise StationCrossSectionV2R3Error("station source domain differs from V1")
    validate_station_source(source_by_id, parent_by_id)

    changed_station_ids = set(
        parent.loc[parent["app_edge_id"].isin(changed_app_edges), "station_id"]
    )
    if changed_station_ids != EXPECTED_CHANGED_STATIONS:
        raise StationCrossSectionV2R3Error(
            f"changed station set differs: {sorted(changed_station_ids)}"
        )
    unchanged_station_ids = set(parent["station_id"]) - changed_station_ids
    if len(unchanged_station_ids) != 420:
        raise StationCrossSectionV2R3Error("unchanged station count differs")
    validate_unchanged_relations(
        parent_by_id,
        unchanged_station_ids,
        relation_v1_by_app,
        relation_v2_by_app,
    )

    preexisting_changed_member_ids = {
        member_id
        for app_edge_id in changed_app_edges
        for member_id in parse_json_strings(
            relation_v1_by_app.loc[app_edge_id, "ordered_sumo_edge_ids_json"]
        )
    }
    frozen_geometries, frozen_lane_ids = load_edge_map_geometries(
        edge_map_path, preexisting_changed_member_ids
    )
    supplement = pq.read_table(geometry_parquet_path).to_pandas()
    supplement["sumo_edge_id"] = supplement["sumo_edge_id"].astype(str)
    supplement_by_id = supplement.set_index("sumo_edge_id", drop=False)
    synthetic_member_ids = set(supplement_by_id.index)
    if synthetic_member_ids != set(geometry_manifest.synthetic_member_ids):
        raise StationCrossSectionV2R3Error("synthetic geometry member set differs")
    supplement_geometries = {
        edge_id: wkb.loads(supplement_by_id.loc[edge_id, "projected_geometry_wkb"])
        for edge_id in synthetic_member_ids
    }
    supplement_lane_ids = {
        edge_id: parse_json_strings(supplement_by_id.loc[edge_id, "lane_ids_json"])
        for edge_id in synthetic_member_ids
    }
    member_lengths = relation_member_lengths(
        relation_v2.loc[relation_v2["app_edge_id"].isin(changed_app_edges)]
    )

    candidate = parent.copy(deep=True).set_index("station_id", drop=False)
    provenance_rows: list[dict[str, Any]] = []
    for station_id in sorted(unchanged_station_ids):
        app_edge_id = str(parent_by_id.loc[station_id, "app_edge_id"])
        relation = relation_v1_by_app.loc[app_edge_id] if app_edge_id in relation_v1_by_app.index else None
        provenance_rows.append(
            {
                "station_id": station_id,
                "app_edge_id": app_edge_id,
                "disposition": "INHERITED_FROZEN_V1_ROW_EXACT",
                "parent_relation_id": None if relation is None else str(relation["relation_id"]),
                "candidate_relation_id": None if relation is None else str(relation["relation_id"]),
                "ordered_sumo_edge_ids_json": "[]" if relation is None else str(relation["ordered_sumo_edge_ids_json"]),
                "geometry_sources_json": "[]",
                "projection_executed": False,
                "unchanged_member_geometry_reconstructed": False,
                "coordinate_source": "FROZEN_STATION_CROSS_SECTIONS_V1_BINARY64",
            }
        )

    projection_fields = (
        "sumo_edge_id",
        "chain_member_index",
        "projected_position_m",
        "normalized_position",
        "chain_position_m",
        "projection_distance_m",
        "member_length_m",
        "distance_to_upstream_member_boundary_m",
        "distance_to_downstream_member_boundary_m",
        "nearest_boundary_distance_m",
        "effectively_equal_member_count",
        "candidate_member_count",
        "direction_compatible",
        "projection_status",
        "projection_reason",
    )
    for station_id in sorted(changed_station_ids):
        parent_row = parent_by_id.loc[station_id]
        app_edge_id = str(parent_row["app_edge_id"])
        relation_v1_row = relation_v1_by_app.loc[app_edge_id]
        relation_v2_row = relation_v2_by_app.loc[app_edge_id]
        parent_chain = parse_json_strings(relation_v1_row["ordered_sumo_edge_ids_json"])
        candidate_chain = parse_json_strings(relation_v2_row["ordered_sumo_edge_ids_json"])
        added = set(parse_json_strings(relation_v2_row["synthetic_boundary_member_ids_json"]))
        if added != set(candidate_chain) - set(parent_chain) or not added.issubset(synthetic_member_ids):
            raise StationCrossSectionV2R3Error(f"synthetic chain delta differs for {app_edge_id}")
        geometries: dict[str, LineString] = {}
        lane_ids: dict[str, list[str]] = {}
        geometry_sources: list[dict[str, Any]] = []
        for edge_id in candidate_chain:
            if edge_id in added:
                geometry = supplement_geometries[edge_id]
                geometries[edge_id] = geometry
                lane_ids[edge_id] = supplement_lane_ids[edge_id]
                geometry_sources.append(
                    {
                        "sumo_edge_id": edge_id,
                        "source": "SYNTHETIC_BOUNDARY_MEMBER_GEOMETRIES_V1",
                        "wkb_sha256": hashlib.sha256(bytes(geometry.wkb)).hexdigest(),
                    }
                )
            else:
                if edge_id not in parent_chain or edge_id not in frozen_geometries:
                    raise StationCrossSectionV2R3Error(
                        f"pre-existing member lacks frozen edge-map geometry: {edge_id}"
                    )
                geometry = frozen_geometries[edge_id]
                geometries[edge_id] = geometry
                lane_ids[edge_id] = frozen_lane_ids[edge_id]
                geometry_sources.append(
                    {
                        "sumo_edge_id": edge_id,
                        "source": "FROZEN_EDGE_MAP_WKB",
                        "wkb_sha256": hashlib.sha256(bytes(geometry.wkb)).hexdigest(),
                    }
                )
        projection = project_station_to_relation(
            station_id=station_id,
            longitude=float(parent_row["station_longitude"]),
            latitude=float(parent_row["station_latitude"]),
            direction=str(parent_row["direction"]),
            relation=relation_v2_row.to_dict(),
            member_geometries=geometries,
            member_lengths_m=member_lengths,
        )
        candidate.loc[station_id, "schema_version"] = 2
        candidate.loc[station_id, "mapping_id"] = mapping_id(
            station_id, app_edge_id, str(relation_v2_row["relation_id"])
        )
        candidate.loc[station_id, "relation_id"] = str(relation_v2_row["relation_id"])
        candidate.loc[station_id, "relation_class"] = relation_v2_row["relation_class"]
        for field in projection_fields:
            candidate.loc[station_id, field] = projection.get(field)
        selected = projection.get("sumo_edge_id")
        selected_lanes = [] if selected is None else lane_ids[str(selected)]
        candidate.loc[station_id, "sumo_lane_ids_json"] = canonical_json(selected_lanes)
        candidate.loc[station_id, "sumo_lane_count"] = len(selected_lanes)
        candidate.loc[station_id, "station_longitude"] = parent_row["station_longitude"]
        candidate.loc[station_id, "station_latitude"] = parent_row["station_latitude"]
        candidate.loc[station_id, "projection_algorithm_version"] = STATION_CROSS_SECTION_ALGORITHM_VERSION
        provenance_rows.append(
            {
                "station_id": station_id,
                "app_edge_id": app_edge_id,
                "disposition": "RECOMPUTED_CHANGED_COMPARISON_V2_RELATION",
                "parent_relation_id": str(relation_v1_row["relation_id"]),
                "candidate_relation_id": str(relation_v2_row["relation_id"]),
                "ordered_sumo_edge_ids_json": canonical_json(candidate_chain),
                "geometry_sources_json": canonical_json(geometry_sources),
                "projection_executed": True,
                "unchanged_member_geometry_reconstructed": False,
                "coordinate_source": "FROZEN_STATION_CROSS_SECTIONS_V1_BINARY64",
            }
        )

    candidate = candidate.reset_index(drop=True).sort_values(
        "station_id", kind="mergesort"
    ).reset_index(drop=True)
    row_provenance = pd.DataFrame(provenance_rows).sort_values(
        "station_id", kind="mergesort"
    ).reset_index(drop=True)
    ensure_unchanged_rows_exact(parent_by_id, candidate.set_index("station_id"), unchanged_station_ids)
    summary = {
        "artifact_status": "complete_characterization_pending_acceptance_policy",
        "repair_classification": "D_MIXED_RUNTIME_AND_IMPLEMENTATION_EFFECT",
        "station_count": len(candidate),
        "unchanged_station_count": len(unchanged_station_ids),
        "changed_station_count": len(changed_station_ids),
        "changed_app_edge_ids": sorted(changed_app_edges),
        "changed_station_ids": sorted(changed_station_ids),
        "unchanged_row_contract": "inherit_frozen_station_cross_sections_v1_row_exact",
        "changed_row_contract": "recompute_only_comparison_v2_changed_app_relations",
        "geometry_contract": "frozen_edge_map_wkb_for_existing_members_plus_frozen_synthetic_boundary_geometry_v1",
        "scientific_guards": {
            "traffic_values_loaded": False,
            "sumo_behavior_executed": False,
            "development_loaded": False,
            "blind_loaded": False,
            "tolerance_or_rounding_introduced": False,
            "unchanged_rows_reprojected": False,
            "unchanged_member_geometry_reconstructed": False,
            "projection_methodology_modified": False,
            "station_eligibility_modified": False,
            "comparison_relations_modified": False,
            "sumo_network_modified": False,
        },
    }
    report = render_report(summary)

    pending.mkdir(parents=True)
    mapping_path = pending / MAPPING
    provenance_path = pending / ROW_PROVENANCE
    summary_path = pending / SUMMARY
    report_path = pending / REPORT
    e1_path = pending / E1
    manifest_path = pending / MANIFEST
    pq.write_table(
        pa.Table.from_pandas(candidate, preserve_index=False).replace_schema_metadata(
            {
                b"artifact_schema_version": b"2",
                b"artifact_revision": b"r3",
                b"row_schema_policy": b"inherited-v1-or-recomputed-v2",
            }
        ),
        mapping_path,
        compression="zstd",
        compression_level=9,
    )
    pq.write_table(
        pa.Table.from_pandas(row_provenance, preserve_index=False),
        provenance_path,
        compression="zstd",
        compression_level=9,
    )
    summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    shutil.copyfile(parent_e1_path, e1_path)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "artifact_revision": "r3",
        "artifact_type": "commute_help_station_sumo_cross_section_analysis",
        "artifact_status": "complete_characterization_pending_acceptance_policy",
        "calibration_status": "not_calibrated",
        "generated_at": datetime.now(UTC).isoformat(),
        "producer_name": "portal_station_sumo_cross_section_projector_v2_r3",
        "producer_version": "phase-2.2b-v2-r3",
        "projection_algorithm_version": STATION_CROSS_SECTION_ALGORITHM_VERSION,
        "repair_classification": "D_MIXED_RUNTIME_AND_IMPLEMENTATION_EFFECT",
        "unchanged_row_contract": "inherit_frozen_station_cross_sections_v1_row_exact",
        "changed_row_contract": "recompute_only_comparison_v2_changed_app_relations",
        "geometry_contract": "frozen_edge_map_wkb_for_existing_members_plus_frozen_synthetic_boundary_geometry_v1",
        "coordinate_representation_contract": "inherit_frozen_station_cross_sections_v1_binary64_coordinates",
        "row_schema_policy": "inherited_rows_retain_v1_row_schema_version_changed_rows_use_v2_row_schema_version",
        "parent_manifest_sha256": sha256_file(parent_manifest_path),
        "parent_mapping_sha256": sha256_file(parent_mapping_path),
        "parent_content_digest": parent_manifest.content_digest,
        "rejected_candidate_manifest_sha256": sha256_file(rejected_manifest_path),
        "rejected_candidate_mapping_sha256": sha256_file(rejected_mapping_path),
        "rejected_candidate_content_digest": str(rejected_manifest["content_digest"]),
        "comparison_v1_manifest_sha256": sha256_file(comparison_v1_manifest_path),
        "comparison_v1_parquet_sha256": sha256_file(comparison_v1_parquet_path),
        "comparison_v2_manifest_sha256": sha256_file(comparison_v2_manifest_path),
        "comparison_v2_parquet_sha256": sha256_file(comparison_v2_parquet_path),
        "comparison_v2_content_digest": str(comparison_v2_manifest["content_digest"]),
        "edge_map_sha256": sha256_file(edge_map_path),
        "station_mapping_sha256": sha256_file(station_mapping_path),
        "synthetic_geometry_manifest_sha256": sha256_file(geometry_manifest_path),
        "synthetic_geometry_parquet_sha256": sha256_file(geometry_parquet_path),
        "synthetic_geometry_validation_summary_sha256": sha256_file(
            synthetic_geometry_validation_summary_path
        ),
        "synthetic_geometry_validation_provenance_sha256": sha256_file(
            geometry_validation_provenance_path
        ),
        "abc_diagnostic_summary_sha256": sha256_file(abc_summary_path),
        "abc_diagnostic_provenance_sha256": sha256_file(abc_provenance_path),
        "unchanged_station_count": 420,
        "changed_station_count": 6,
        "changed_app_edge_ids": sorted(changed_app_edges),
        "changed_station_ids": sorted(changed_station_ids),
        "scientific_guards": summary["scientific_guards"],
        "mapping_output": file_identity(mapping_path, len(candidate)),
        "row_provenance_output": file_identity(provenance_path, len(row_provenance)),
        "summary_output": file_identity(summary_path),
        "report_output": file_identity(report_path),
        "e1_feasibility_output": file_identity(e1_path),
        "row_content_sha256": frame_digest(candidate),
    }
    payload["content_digest"] = content_digest(payload)
    manifest = StationCrossSectionManifestV2R3.model_validate(payload)
    manifest_path.write_text(
        canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    os.replace(pending, output_directory)
    return manifest


def read_station_frame(path: Path) -> pd.DataFrame:
    frame = pq.read_table(path).to_pandas()
    frame["station_id"] = frame["station_id"].astype(str)
    frame["app_edge_id"] = frame["app_edge_id"].astype(str)
    if not frame["station_id"].is_unique:
        raise StationCrossSectionV2R3Error(f"duplicate station IDs: {path}")
    return frame.sort_values("station_id", kind="mergesort").reset_index(drop=True)


def read_relation_frame(path: Path) -> pd.DataFrame:
    frame = pq.read_table(path).to_pandas()
    frame["app_edge_id"] = frame["app_edge_id"].astype(str)
    if not frame["app_edge_id"].is_unique:
        raise StationCrossSectionV2R3Error(f"duplicate APP edges: {path}")
    return frame


def validate_station_source(source: pd.DataFrame, parent: pd.DataFrame) -> None:
    for station_id in sorted(parent.index):
        source_row = source.loc[station_id]
        parent_row = parent.loc[station_id]
        checks = {
            "app_edge": str(source_row["edge_id"]) == str(parent_row["app_edge_id"]),
            "direction": str(source_row["direction"]) == str(parent_row["direction"]),
            "longitude_token": finite_decimal(source_row["longitude"]),
            "latitude_token": finite_decimal(source_row["latitude"]),
            "parent_longitude": math.isfinite(float(parent_row["station_longitude"])),
            "parent_latitude": math.isfinite(float(parent_row["station_latitude"])),
        }
        if not all(checks.values()):
            failed = sorted(key for key, value in checks.items() if not value)
            raise StationCrossSectionV2R3Error(
                f"station source lineage differs for {station_id}: {failed}"
            )


def validate_unchanged_relations(
    parent: pd.DataFrame,
    station_ids: set[str],
    relation_v1: pd.DataFrame,
    relation_v2: pd.DataFrame,
) -> None:
    for station_id in sorted(station_ids):
        app_edge_id = str(parent.loc[station_id, "app_edge_id"])
        first_exists = app_edge_id in relation_v1.index
        second_exists = app_edge_id in relation_v2.index
        if first_exists != second_exists:
            raise StationCrossSectionV2R3Error(
                f"unchanged relation presence differs: {app_edge_id}"
            )
        if not first_exists:
            continue
        first = relation_v1.loc[app_edge_id]
        second = relation_v2.loc[app_edge_id]
        if str(first["relation_id"]) != str(second["relation_id"]):
            raise StationCrossSectionV2R3Error(
                f"unchanged relation ID differs: {app_edge_id}"
            )
        if parse_json_strings(first["ordered_sumo_edge_ids_json"]) != parse_json_strings(
            second["ordered_sumo_edge_ids_json"]
        ):
            raise StationCrossSectionV2R3Error(
                f"unchanged relation chain differs: {app_edge_id}"
            )


def load_edge_map_geometries(
    path: Path, required_member_ids: set[str]
) -> tuple[dict[str, LineString], dict[str, list[str]]]:
    frame = pq.read_table(
        path,
        columns=["sumo_edge_id", "status", "sumo_geometry_wkb", "sumo_lane_ids"],
    ).to_pandas()
    frame["sumo_edge_id"] = frame["sumo_edge_id"].astype(str)
    frame = frame.loc[
        frame["status"].eq("accepted")
        & frame["sumo_edge_id"].isin(required_member_ids)
    ].drop_duplicates("sumo_edge_id")
    if set(frame["sumo_edge_id"]) != required_member_ids:
        missing = sorted(required_member_ids - set(frame["sumo_edge_id"]))
        raise StationCrossSectionV2R3Error(
            f"changed-chain frozen edge-map geometry is incomplete: {missing}"
        )
    geometries = {
        str(row.sumo_edge_id): wkb.loads(row.sumo_geometry_wkb)
        for row in frame.itertuples(index=False)
    }
    lane_ids = {
        str(row.sumo_edge_id): parse_lane_ids(row.sumo_lane_ids)
        for row in frame.itertuples(index=False)
    }
    return geometries, lane_ids


def relation_member_lengths(relations: pd.DataFrame) -> dict[str, float]:
    result: dict[str, float] = {}
    for value in relations["member_topology_evidence_json"]:
        for evidence in json.loads(str(value)):
            if evidence.get("length_m") is not None:
                result[str(evidence["sumo_edge_id"])] = float(evidence["length_m"])
    return result


def ensure_unchanged_rows_exact(
    parent: pd.DataFrame, candidate: pd.DataFrame, station_ids: set[str]
) -> None:
    if list(parent.columns) != list(candidate.columns):
        raise StationCrossSectionV2R3Error("candidate mapping columns differ from V1")
    for station_id in sorted(station_ids):
        for column in parent.columns:
            if not values_equal(parent.loc[station_id, column], candidate.loc[station_id, column]):
                raise StationCrossSectionV2R3Error(
                    f"unchanged row differs for {station_id}: {column}"
                )


def mapping_id(station_id: str, app_edge_id: str, relation_id: str) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "station_id": station_id,
                "app_edge_id": app_edge_id,
                "relation_id": relation_id,
                "projection_algorithm": STATION_CROSS_SECTION_ALGORITHM_VERSION,
            }
        ).encode()
    ).hexdigest()


def finite_decimal(value: Any) -> bool:
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError):
        return False


def parse_json_strings(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise StationCrossSectionV2R3Error("expected JSON list")
    return [str(item) for item in parsed]


def parse_lane_ids(value: Any) -> list[str]:
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return sorted(str(item) for item in parsed)
    except json.JSONDecodeError:
        pass
    return sorted(item for item in str(value).replace(",", " ").split() if item)


def values_equal(first: Any, second: Any) -> bool:
    first_missing = bool(pd.isna(first))
    second_missing = bool(pd.isna(second))
    if first_missing and second_missing:
        return True
    if first_missing or second_missing:
        return False
    return first == second


def render_report(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Station cross-sections V2-R3 dependency-local repair",
            "",
            "Frozen V1 rows are inherited exactly for unchanged comparison relations.",
            "Only six stations on the three comparison-v2 repaired relations are reprojected.",
            "Existing chain members use frozen edge-map WKB; three new synthetic members use the separately frozen geometry supplement.",
            "",
            f"- Station rows: {summary['station_count']}",
            f"- Inherited rows: {summary['unchanged_station_count']}",
            f"- Recomputed rows: {summary['changed_station_count']}",
            "- Traffic values loaded: no",
            "- SUMO behavior executed: no",
            "",
        ]
    )


def require_sha(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise StationCrossSectionV2R3Error(
            f"SHA-256 mismatch for {path}: expected {expected}, found {actual}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path, rows: int | None = None) -> dict[str, Any]:
    return {
        "relative_path": path.name,
        "sha256": sha256_file(path),
        "byte_count": path.stat().st_size,
        "row_count": rows,
    }


def frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        normalized = {
            key: None if pd.isna(value) else value.item() if hasattr(value, "item") else value
            for key, value in row.items()
        }
        digest.update(canonical_json(normalized).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
