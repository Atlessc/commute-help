"""Propagate accepted station-cross-sections-v2-r3 into policy-v2."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.sumo_comparison_relations import ComparisonRelationManifestV2
from backend.app.schemas.sumo_station_cross_section_policy import (
    POLICY_ALGORITHM_VERSION,
    POLICY_DEPENDENCY_CONTRACT_V2,
    POLICY_NAME,
    POLICY_SCHEMA_VERSION_V2,
    POLICY_VERSION_V2,
    PolicyOutputV1,
    StationCrossSectionPolicyManifestV1,
    StationCrossSectionPolicyManifestV2,
)
from backend.app.schemas.sumo_station_cross_sections_v2_r3 import (
    StationCrossSectionManifestV2R3,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.station_cross_section_policy_service import (
    BOUNDARY_REVIEW_DISTANCE_M,
    BOUNDARY_TRANSITION_TOLERANCE_M,
    MAX_PROJECTION_DISTANCE_M,
    classify_projection,
)

POLICY_PARQUET = "station-cross-section-policy.parquet"
REVIEW_PARQUET = "station-cross-section-review.parquet"
SUMMARY_JSON = "station-cross-section-policy-summary.json"
REPORT_MD = "STATION_CROSS_SECTION_POLICY.md"
MANIFEST_JSON = "station-cross-section-policy-manifest.json"

EXPECTED_CHANGED_STATIONS = {
    "portal-station-1013",
    "portal-station-1060",
    "portal-station-3117",
    "portal-station-3118",
    "portal-station-3132",
    "portal-station-3194",
}
EXPECTED_CHANGED_EDGES = {
    "58982054bb2b4202e8b09b04",
    "6bbb95754afd8b91412741d2",
    "a5c8af67423f92712af0a5ab",
}
EXPECTED_GRAPH_VERSION = "2026-08-08-portland-vancouver-frozen-v2"
EXPECTED_SUMO_NETWORK_VERSION = "pv-sumo-2026-08-08-v1"
OUTCOME_FIELDS = (
    "policy_state",
    "cross_section_type",
    "accepted_geometry",
    "flow_measurement_eligible",
    "speed_measurement_eligible",
    "decision_reason",
    "transition_from_sumo_edge_id",
    "transition_to_sumo_edge_id",
)
POLICY_COLUMNS = (
    "schema_version",
    "policy_row_id",
    "source_mapping_id",
    "station_id",
    "app_edge_id",
    "relation_id",
    "relation_class",
    "sumo_edge_id",
    "chain_member_index",
    "projected_position_m",
    "chain_position_m",
    "projection_distance_m",
    "nearest_boundary_distance_m",
    "distance_to_upstream_member_boundary_m",
    "distance_to_downstream_member_boundary_m",
    "direction",
    "direction_compatible",
    "highway_name",
    "location_text",
    "station_longitude",
    "station_latitude",
    "station_app_mapping_confidence",
    "station_app_mapping_distance_m",
    "station_app_mapping_score",
    "source_detector_count",
    "historical_profile_participation_count",
    "stations_on_app_edge",
    "road_name",
    "road_ref",
    "road_class",
    "direction_signature",
    *OUTCOME_FIELDS,
    "speed_policy_reason",
    "policy_version",
    "policy_algorithm_version",
)


class StationCrossSectionPolicyV2Error(RuntimeError):
    """Policy-v2 lineage or dependency-local semantics are invalid."""


def build_station_cross_section_policy_v2(
    *,
    parent_policy_directory: Path,
    parent_cross_section_directory: Path,
    source_cross_section_directory: Path,
    source_cross_section_validation_summary_path: Path,
    relation_directory: Path,
    output_directory: Path,
) -> StationCrossSectionPolicyManifestV2:
    """Build policy-v2 without reading traffic or reprojecting stations."""
    if output_directory.exists():
        raise StationCrossSectionPolicyV2Error(
            f"refusing existing output artifact: {output_directory}"
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        raise StationCrossSectionPolicyV2Error(
            f"refusing existing pending artifact: {pending}"
        )

    parent_manifest_path = parent_policy_directory / MANIFEST_JSON
    parent_manifest = StationCrossSectionPolicyManifestV1.model_validate_json(
        parent_manifest_path.read_text(encoding="utf-8")
    )
    source_manifest_path = (
        source_cross_section_directory / "station-cross-section-manifest.json"
    )
    source_manifest = StationCrossSectionManifestV2R3.model_validate_json(
        source_manifest_path.read_text(encoding="utf-8")
    )
    relation_manifest_path = relation_directory / "comparison-relation-manifest.json"
    relation_manifest = ComparisonRelationManifestV2.model_validate_json(
        relation_manifest_path.read_text(encoding="utf-8")
    )
    validation = json.loads(
        source_cross_section_validation_summary_path.read_text(encoding="utf-8")
    )
    _require(
        validation.get("status") == "PASS_TRAFFIC_BLIND_STATIC_VALIDATION",
        "source cross-section artifact lacks accepted validation",
    )
    validation_changed_stations = {
        str(row["station_id"])
        for row in validation.get("changed_rows", [])
        if isinstance(row, dict) and row.get("station_id") is not None
    }
    _require(
        validation.get("changed_station_count") == 6
        and validation_changed_stations == EXPECTED_CHANGED_STATIONS,
        "accepted source changed-station domain differs",
    )
    _require(
        set(source_manifest.changed_station_ids) == EXPECTED_CHANGED_STATIONS
        and set(source_manifest.changed_app_edge_ids) == EXPECTED_CHANGED_EDGES,
        "source manifest dependency domain differs",
    )

    parent_policy_path = parent_policy_directory / parent_manifest.policy_output.relative_path
    parent_cross_manifest = json.loads(
        (parent_cross_section_directory / "station-cross-section-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    parent_cross_path = (
        parent_cross_section_directory
        / parent_cross_manifest["mapping_output"]["relative_path"]
    )
    source_path = source_cross_section_directory / source_manifest.mapping_output.relative_path
    relation_path = relation_directory / relation_manifest.relation_output.relative_path
    _require(_sha256(parent_policy_path) == parent_manifest.policy_output.sha256, "parent policy hash differs")
    _require(_sha256(parent_cross_path) == source_manifest.parent_mapping_sha256, "parent cross-section hash differs")
    _require(_sha256(source_path) == source_manifest.mapping_output.sha256, "source cross-section hash differs")
    _require(_sha256(relation_path) == relation_manifest.relation_output.sha256, "comparison-v2 hash differs")
    _require(source_manifest.comparison_v2_manifest_sha256 == _sha256(relation_manifest_path), "source/comparison-v2 manifest lineage differs")
    _require(source_manifest.comparison_v2_parquet_sha256 == relation_manifest.relation_output.sha256, "source/relation lineage differs")
    _require(source_manifest.comparison_v2_content_digest == relation_manifest.content_digest, "source/comparison-v2 content lineage differs")
    _require(
        relation_manifest.graph_version
        == parent_manifest.graph_version
        == parent_cross_manifest.get("graph_version")
        == EXPECTED_GRAPH_VERSION,
        "graph-version lineage differs across comparison-v2, policy-v1, parent cross-sections, or frozen network family",
    )
    _require(
        relation_manifest.sumo_network_version
        == parent_manifest.sumo_network_version
        == parent_cross_manifest.get("sumo_network_version")
        == EXPECTED_SUMO_NETWORK_VERSION,
        "SUMO-network-version lineage differs across comparison-v2, policy-v1, parent cross-sections, or frozen network family",
    )

    parent_policy = _station_frame(parent_policy_path)
    parent_cross = _station_frame(parent_cross_path)
    source = _station_frame(source_path)
    _require(len(parent_policy) == len(parent_cross) == len(source) == 426, "station count differs from frozen 426-row domain")
    _require(set(parent_policy.index) == set(parent_cross.index) == set(source.index), "station domains differ")
    _require(list(parent_policy.columns) == list(POLICY_COLUMNS), "parent policy row schema differs")

    relations = pq.read_table(
        relation_path,
        columns=[
            "app_edge_id",
            "ordered_sumo_edge_ids_json",
            "road_name",
            "ref",
            "road_class",
            "direction_signature",
        ],
    ).to_pandas()
    relations["app_edge_id"] = relations["app_edge_id"].astype(str)
    _require(not relations["app_edge_id"].duplicated().any(), "comparison-v2 APP-edge identities are not unique")
    relation_lookup = relations.set_index("app_edge_id").to_dict(orient="index")

    actual_source_changes = {
        station_id
        for station_id in source.index
        if not _rows_equal(source.loc[station_id], parent_cross.loc[station_id])
    }
    _require(actual_source_changes == EXPECTED_CHANGED_STATIONS, "source changes escape accepted six-station dependency set")

    rows: list[dict[str, Any]] = []
    outcome_changes: list[dict[str, Any]] = []
    for station_id in sorted(source.index):
        source_row = source.loc[station_id].to_dict()
        parent_row = parent_policy.loc[station_id].to_dict()
        if station_id not in EXPECTED_CHANGED_STATIONS:
            row = dict(parent_row)
        else:
            relation = relation_lookup.get(str(source_row["app_edge_id"]))
            _require(relation is not None, f"comparison-v2 relation missing for {station_id}")
            ordered = _parse_strings(relation["ordered_sumo_edge_ids_json"])
            decision = classify_projection(source_row, ordered)
            row = {
                "schema_version": POLICY_SCHEMA_VERSION_V2,
                "policy_row_id": "",
                "source_mapping_id": source_row["mapping_id"],
                "station_id": source_row["station_id"],
                "app_edge_id": source_row["app_edge_id"],
                "relation_id": source_row["relation_id"],
                "relation_class": source_row["relation_class"],
                "sumo_edge_id": source_row["sumo_edge_id"],
                "chain_member_index": source_row["chain_member_index"],
                "projected_position_m": source_row["projected_position_m"],
                "chain_position_m": source_row["chain_position_m"],
                "projection_distance_m": source_row["projection_distance_m"],
                "nearest_boundary_distance_m": source_row["nearest_boundary_distance_m"],
                "distance_to_upstream_member_boundary_m": source_row["distance_to_upstream_member_boundary_m"],
                "distance_to_downstream_member_boundary_m": source_row["distance_to_downstream_member_boundary_m"],
                "direction": source_row["direction"],
                "direction_compatible": bool(source_row["direction_compatible"]),
                "highway_name": source_row["highway_name"],
                "location_text": source_row["location_text"],
                "station_longitude": source_row["station_longitude"],
                "station_latitude": source_row["station_latitude"],
                "station_app_mapping_confidence": source_row["station_app_mapping_confidence"],
                "station_app_mapping_distance_m": source_row["station_app_mapping_distance_m"],
                "station_app_mapping_score": source_row["station_app_mapping_score"],
                "source_detector_count": source_row["source_detector_count"],
                "historical_profile_participation_count": source_row["historical_profile_participation_count"],
                "stations_on_app_edge": source_row["stations_on_app_edge"],
                "road_name": relation.get("road_name"),
                "road_ref": relation.get("ref"),
                "road_class": relation.get("road_class"),
                "direction_signature": relation.get("direction_signature"),
                **decision,
                "speed_policy_reason": parent_row["speed_policy_reason"],
                "policy_version": POLICY_VERSION_V2,
                "policy_algorithm_version": POLICY_ALGORITHM_VERSION,
            }
            old_outcome = {field: _json_value(parent_row[field]) for field in OUTCOME_FIELDS}
            new_outcome = {field: _json_value(row[field]) for field in OUTCOME_FIELDS}
            if old_outcome != new_outcome:
                outcome_changes.append(
                    {
                        "station_id": station_id,
                        "app_edge_id": str(source_row["app_edge_id"]),
                        "old": old_outcome,
                        "new": new_outcome,
                    }
                )
        if station_id in EXPECTED_CHANGED_STATIONS:
            row["schema_version"] = POLICY_SCHEMA_VERSION_V2
            row["policy_version"] = POLICY_VERSION_V2
            row["policy_algorithm_version"] = POLICY_ALGORITHM_VERSION
            row["policy_row_id"] = _policy_row_id(str(row["source_mapping_id"]))
        rows.append({column: row[column] for column in POLICY_COLUMNS})

    frame = pd.DataFrame(rows, columns=POLICY_COLUMNS).sort_values("station_id", kind="mergesort").reset_index(drop=True)
    _require(len(frame) == 426 and frame["policy_row_id"].is_unique, "candidate policy identity is invalid")
    review = frame.loc[~frame["accepted_geometry"].copy()].copy()
    policy_semantic_changed = {item["station_id"] for item in outcome_changes}
    _require(policy_semantic_changed.issubset(EXPECTED_CHANGED_STATIONS), "policy outcome changed outside dependency set")
    parent_eligible = set(parent_policy.loc[parent_policy["accepted_geometry"].map(bool)].index)
    candidate_indexed = frame.set_index("station_id", drop=False)
    candidate_eligible = set(candidate_indexed.loc[candidate_indexed["accepted_geometry"].map(bool)].index)
    summary = {
        "source_station_count": 426,
        "parent_policy_row_count": 426,
        "candidate_policy_row_count": 426,
        "station_domain_equal": True,
        "dependency_inherited_station_count": 420,
        "dependency_recomputed_station_count": 6,
        "dependency_changed_station_ids": sorted(EXPECTED_CHANGED_STATIONS),
        "dependency_changed_app_edge_ids": sorted(EXPECTED_CHANGED_EDGES),
        "inherited_rows_exact_field_for_field": True,
        "row_schema_policy": "inherited_rows_retain_v1_identity_changed_rows_use_v2_identity",
        "policy_semantic_unchanged_row_count": 426 - len(outcome_changes),
        "policy_semantic_changed_row_count": len(outcome_changes),
        "policy_semantic_changes": outcome_changes,
        "accepted_geometry_station_count": int(frame["accepted_geometry"].sum()),
        "flow_measurement_eligible_station_count": int(frame["flow_measurement_eligible"].sum()),
        "speed_measurement_eligible_station_count": int(frame["speed_measurement_eligible"].sum()),
        "eligible_station_membership_changed": parent_eligible != candidate_eligible,
        "eligible_station_ids_added": sorted(candidate_eligible - parent_eligible),
        "eligible_station_ids_removed": sorted(parent_eligible - candidate_eligible),
        "policy_state_counts": {str(key): int(value) for key, value in frame["policy_state"].value_counts().sort_index().items()},
        "historical_coverage_summary_recomputed": False,
        "historical_coverage_reason": "traffic_blind_structural_prerequisite_propagation_only",
        "structural_mapping_v2_readiness": "READY_IF_INDEPENDENT_POLICY_V2_VALIDATION_PASSES",
        "level_1_status": "NOT_PROMOTED",
        "scientific_guards": _guards(),
    }
    report = _render_report(summary)
    rules = {
        "maximum_projection_distance_m": MAX_PROJECTION_DISTANCE_M,
        "boundary_transition_tolerance_m": BOUNDARY_TRANSITION_TOLERANCE_M,
        "boundary_review_distance_m": BOUNDARY_REVIEW_DISTANCE_M,
        "direction_required": True,
        "internal_sequential_member_boundary_is_edge_transition": True,
        "point_speed_eligible": False,
    }

    pending.mkdir(parents=True)
    try:
        policy_path = pending / POLICY_PARQUET
        review_path = pending / REVIEW_PARQUET
        summary_path = pending / SUMMARY_JSON
        report_path = pending / REPORT_MD
        _write_parquet(frame, policy_path, len(frame))
        _write_parquet(review, review_path, len(review))
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        row_digest = _frame_digest(frame)
        envelope = {
            "schema_version": POLICY_SCHEMA_VERSION_V2,
            "artifact_type": "commute_help_station_cross_section_acceptance_policy",
            "artifact_status": "complete_policy",
            "calibration_status": "not_calibrated",
            "policy_name": POLICY_NAME,
            "policy_version": POLICY_VERSION_V2,
            "policy_algorithm_version": POLICY_ALGORITHM_VERSION,
            "dependency_contract": POLICY_DEPENDENCY_CONTRACT_V2,
            "row_schema_policy": "inherited_rows_retain_v1_identity_changed_rows_use_v2_identity",
            "parent_policy_manifest_sha256": _sha256(parent_manifest_path),
            "parent_policy_content_digest": parent_manifest.content_digest,
            "parent_policy_parquet_sha256": _sha256(parent_policy_path),
            "source_station_cross_section_manifest_sha256": _sha256(source_manifest_path),
            "source_station_cross_section_validation_sha256": _sha256(source_cross_section_validation_summary_path),
            "source_station_cross_section_digest": source_manifest.content_digest,
            "source_station_cross_section_sha256": _sha256(source_path),
            "source_relation_manifest_sha256": _sha256(relation_manifest_path),
            "source_relation_digest": relation_manifest.content_digest,
            "source_relation_parquet_sha256": _sha256(relation_path),
            "graph_version": relation_manifest.graph_version,
            "sumo_network_version": relation_manifest.sumo_network_version,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "decision_rules": rules,
            "dependency_changed_station_ids": sorted(EXPECTED_CHANGED_STATIONS),
            "dependency_changed_app_edge_ids": sorted(EXPECTED_CHANGED_EDGES),
            "inherited_station_count": 420,
            "recomputed_station_count": 6,
            "historical_coverage_summary_recomputed": False,
            "policy_output": _output(policy_path, len(frame)),
            "review_output": _output(review_path, len(review)),
            "summary_output": _output(summary_path, None),
            "report_output": _output(report_path, None),
            "row_content_sha256": row_digest,
            "scientific_guards": _guards(),
        }
        identity = dict(envelope)
        for key in ("generated_at", "policy_output", "review_output", "summary_output", "report_output"):
            identity.pop(key)
        envelope["content_digest"] = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        manifest = StationCrossSectionPolicyManifestV2.model_validate(envelope)
        (pending / MANIFEST_JSON).write_text(canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8")
        _promote(pending, output_directory)
        return manifest
    except BaseException:
        raise


def _station_frame(path: Path) -> pd.DataFrame:
    frame = pq.read_table(path).to_pandas()
    frame["station_id"] = frame["station_id"].astype(str)
    _require(not frame["station_id"].duplicated().any(), f"duplicate station IDs in {path}")
    return frame.set_index("station_id", drop=False)


def _rows_equal(left: pd.Series, right: pd.Series) -> bool:
    if list(left.index) != list(right.index):
        return False
    return all(_json_value(left[key]) == _json_value(right[key]) for key in left.index)


def _json_value(value: Any) -> Any:
    return None if pd.isna(value) else value


def _parse_strings(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    _require(isinstance(parsed, list) and parsed, "ordered SUMO chain is empty")
    return [str(item) for item in parsed]


def _policy_row_id(source_mapping_id: str) -> str:
    return hashlib.sha256(canonical_json({"source_mapping_id": source_mapping_id, "policy_version": POLICY_VERSION_V2}).encode()).hexdigest()


def _write_parquet(frame: pd.DataFrame, path: Path, rows: int) -> None:
    table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata({b"schema_version": b"2", b"policy": POLICY_NAME.encode(), b"policy_version": POLICY_VERSION_V2.encode()})
    _require(table.num_rows == rows, "Parquet row count differs before write")
    pq.write_table(table, path, compression="zstd", use_dictionary=False, write_statistics=True)


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        digest.update(canonical_json({key: _json_value(value) for key, value in row.items()}).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _output(path: Path, rows: int | None) -> PolicyOutputV1:
    return PolicyOutputV1(relative_path=path.name, sha256=_sha256(path), byte_count=path.stat().st_size, row_count=rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StationCrossSectionPolicyV2Error(message)


def _guards() -> dict[str, bool]:
    return {
        "traffic_values_loaded": False,
        "development_loaded": False,
        "blind_loaded": False,
        "sumo_behavior_executed": False,
        "stations_reprojected": False,
        "projection_methodology_modified": False,
        "policy_thresholds_modified": False,
        "level_1_methodology_modified": False,
        "tolerance_or_rounding_introduced": False,
    }


def _render_report(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Station cross-section acceptance policy-v2",
            "",
            "Traffic-blind dependency propagation from accepted station-cross-sections-v2-r3.",
            "",
            f"- Stations: {summary['candidate_policy_row_count']}",
            f"- Dependency-local inherited rows: {summary['dependency_inherited_station_count']}",
            f"- Dependency-local recomputed rows: {summary['dependency_recomputed_station_count']}",
            f"- Policy-semantic changes: {summary['policy_semantic_changed_row_count']}",
            f"- Eligible membership changed: {summary['eligible_station_membership_changed']}",
            "",
            "The distance-boundary-direction-v1 rule is unchanged. Historical profile coverage was not recomputed.",
            "",
        ]
    )


def _promote(pending: Path, output: Path) -> None:
    required = {POLICY_PARQUET, REVIEW_PARQUET, SUMMARY_JSON, REPORT_MD, MANIFEST_JSON}
    present = {path.name for path in pending.iterdir()} if pending.is_dir() else set()
    _require(required == present, "policy-v2 pending package is incomplete or contains extras")
    _require(not output.exists(), "policy-v2 output appeared during generation")
    os.replace(pending, output)
