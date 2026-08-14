"""Apply the reviewed Phase 2.2d projection acceptance policy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.sumo_comparison_relations import ComparisonRelationManifestV1
from backend.app.schemas.sumo_station_cross_section_policy import (
    POLICY_ALGORITHM_VERSION,
    POLICY_NAME,
    POLICY_SCHEMA_VERSION,
    POLICY_VERSION,
    PolicyOutputV1,
    StationCrossSectionPolicyManifestV1,
)
from backend.app.schemas.sumo_station_cross_sections import (
    StationCrossSectionManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

POLICY_PARQUET = "station-cross-section-policy.parquet"
REVIEW_PARQUET = "station-cross-section-review.parquet"
SUMMARY_JSON = "station-cross-section-policy-summary.json"
REPORT_MD = "STATION_CROSS_SECTION_POLICY.md"
MANIFEST_JSON = "station-cross-section-policy-manifest.json"

MAX_PROJECTION_DISTANCE_M = 25.0
BOUNDARY_TRANSITION_TOLERANCE_M = 5.0
BOUNDARY_REVIEW_DISTANCE_M = 25.0


class StationCrossSectionPolicyError(RuntimeError):
    """Policy inputs or outputs violate the Phase 2.2d contract."""


def classify_projection(
    row: dict[str, Any], ordered_edge_ids: list[str]
) -> dict[str, Any]:
    """Classify one station without changing source projection evidence."""
    if pd.isna(row.get("sumo_edge_id")) or not row.get("sumo_edge_id"):
        return _decision(
            "unmatched_relation",
            None,
            False,
            "accepted_app_edge_has_no_accepted_sumo_relation",
        )
    if not bool(row.get("direction_compatible")):
        return _decision(
            "review_direction",
            None,
            False,
            "historical_direction_not_compatible_with_accepted_relation",
        )
    projection_distance = float(row["projection_distance_m"])
    if projection_distance > MAX_PROJECTION_DISTANCE_M:
        return _decision(
            "review_projection_distance",
            None,
            False,
            "lateral_projection_distance_exceeds_25_m",
        )
    index = int(row["chain_member_index"])
    upstream = float(row["distance_to_upstream_member_boundary_m"])
    downstream = float(row["distance_to_downstream_member_boundary_m"])
    near_upstream = upstream < BOUNDARY_TRANSITION_TOLERANCE_M
    near_downstream = downstream < BOUNDARY_TRANSITION_TOLERANCE_M
    upstream_internal = near_upstream and index > 0
    downstream_internal = near_downstream and index < len(ordered_edge_ids) - 1
    if upstream_internal or downstream_internal:
        transition_index = index - 1 if upstream_internal else index
        return _decision(
            "accepted_edge_transition",
            "edge_transition",
            True,
            "within_5_m_of_shared_boundary_between_sequential_accepted_members",
            transition_from=ordered_edge_ids[transition_index],
            transition_to=ordered_edge_ids[transition_index + 1],
        )
    nearest = min(upstream, downstream)
    if nearest < BOUNDARY_REVIEW_DISTANCE_M:
        return _decision(
            "review_boundary",
            None,
            False,
            "within_25_m_of_outer_or_non_equivalent_member_boundary",
        )
    return _decision(
        "accepted_within_edge",
        "within_edge_position",
        True,
        "direction_and_relation_accepted_projection_within_distance_and_boundary_limits",
    )


def build_station_cross_section_policy(
    *,
    source_directory: Path,
    relation_directory: Path,
    historical_directory: Path,
    quality_policy_directory: Path,
    output_directory: Path,
) -> StationCrossSectionPolicyManifestV1:
    """Create deterministic policy rows and a bounded review package atomically."""
    if output_directory.exists():
        return StationCrossSectionPolicyManifestV1.model_validate_json(
            (output_directory / MANIFEST_JSON).read_text(encoding="utf-8")
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    source_manifest = StationCrossSectionManifestV1.model_validate_json(
        (source_directory / "station-cross-section-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    relation_manifest = ComparisonRelationManifestV1.model_validate_json(
        (relation_directory / "comparison-relation-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    source_path = source_directory / source_manifest.mapping_output.relative_path
    source = pq.read_table(source_path).to_pandas()
    relations = pq.read_table(
        relation_directory / relation_manifest.relation_output.relative_path,
        columns=[
            "app_edge_id",
            "ordered_sumo_edge_ids_json",
            "road_name",
            "ref",
            "road_class",
            "direction_signature",
        ],
    ).to_pandas()
    relations = relations.drop_duplicates("app_edge_id")
    relation_lookup = relations.set_index("app_edge_id").to_dict(orient="index")
    rows = []
    for raw in source.sort_values("station_id", kind="mergesort").to_dict(
        orient="records"
    ):
        relation = relation_lookup.get(str(raw["app_edge_id"]), {})
        ordered = json.loads(str(relation.get("ordered_sumo_edge_ids_json") or "[]"))
        decision = classify_projection(raw, ordered)
        policy_row_id = hashlib.sha256(
            canonical_json(
                {
                    "source_mapping_id": raw["mapping_id"],
                    "policy_version": POLICY_VERSION,
                }
            ).encode()
        ).hexdigest()
        rows.append(
            {
                "schema_version": POLICY_SCHEMA_VERSION,
                "policy_row_id": policy_row_id,
                "source_mapping_id": raw["mapping_id"],
                "station_id": raw["station_id"],
                "app_edge_id": raw["app_edge_id"],
                "relation_id": raw["relation_id"],
                "relation_class": raw["relation_class"],
                "sumo_edge_id": raw["sumo_edge_id"],
                "chain_member_index": raw["chain_member_index"],
                "projected_position_m": raw["projected_position_m"],
                "chain_position_m": raw["chain_position_m"],
                "projection_distance_m": raw["projection_distance_m"],
                "nearest_boundary_distance_m": raw["nearest_boundary_distance_m"],
                "distance_to_upstream_member_boundary_m": raw[
                    "distance_to_upstream_member_boundary_m"
                ],
                "distance_to_downstream_member_boundary_m": raw[
                    "distance_to_downstream_member_boundary_m"
                ],
                "direction": raw["direction"],
                "direction_compatible": bool(raw["direction_compatible"]),
                "highway_name": raw["highway_name"],
                "location_text": raw["location_text"],
                "station_longitude": raw["station_longitude"],
                "station_latitude": raw["station_latitude"],
                "station_app_mapping_confidence": raw["station_app_mapping_confidence"],
                "station_app_mapping_distance_m": raw["station_app_mapping_distance_m"],
                "station_app_mapping_score": raw["station_app_mapping_score"],
                "source_detector_count": raw["source_detector_count"],
                "historical_profile_participation_count": raw[
                    "historical_profile_participation_count"
                ],
                "stations_on_app_edge": raw["stations_on_app_edge"],
                "road_name": relation.get("road_name"),
                "road_ref": relation.get("ref"),
                "road_class": relation.get("road_class"),
                "direction_signature": relation.get("direction_signature"),
                **decision,
                "speed_policy_reason": "detector_grade_point_speed_not_proven_in_phase_2_2c",
                "policy_version": POLICY_VERSION,
                "policy_algorithm_version": POLICY_ALGORITHM_VERSION,
            }
        )
    frame = (
        pd.DataFrame(rows)
        .sort_values("station_id", kind="mergesort")
        .reset_index(drop=True)
    )
    if len(frame) != len(source) or not frame["policy_row_id"].is_unique:
        raise StationCrossSectionPolicyError(
            "policy must preserve one unique row per source station"
        )
    summary = _build_summary(
        frame=frame,
        historical_directory=historical_directory,
        quality_policy_directory=quality_policy_directory,
        relations=relations,
    )
    review = frame.loc[~frame["accepted_geometry"]].copy()
    report = _render_report(summary)
    pending.mkdir(parents=True)
    try:
        policy_path = pending / POLICY_PARQUET
        review_path = pending / REVIEW_PARQUET
        summary_path = pending / SUMMARY_JSON
        report_path = pending / REPORT_MD
        table = pa.Table.from_pandas(
            frame, preserve_index=False
        ).replace_schema_metadata(
            {
                "schema_version": "1",
                "policy": POLICY_NAME,
                "policy_version": POLICY_VERSION,
            }
        )
        review_table = pa.Table.from_pandas(
            review, preserve_index=False
        ).replace_schema_metadata(
            {
                "schema_version": "1",
                "policy": POLICY_NAME,
                "policy_version": POLICY_VERSION,
            }
        )
        pq.write_table(
            table,
            policy_path,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
        pq.write_table(
            review_table,
            review_path,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        row_digest = _frame_digest(frame)
        rules = {
            "maximum_projection_distance_m": MAX_PROJECTION_DISTANCE_M,
            "boundary_transition_tolerance_m": BOUNDARY_TRANSITION_TOLERANCE_M,
            "boundary_review_distance_m": BOUNDARY_REVIEW_DISTANCE_M,
            "direction_required": True,
            "internal_sequential_member_boundary_is_edge_transition": True,
            "all_profile_source_stations_required_for_direct_flow_coverage": True,
            "point_speed_eligible": False,
        }
        envelope = {
            "schema_version": 1,
            "artifact_type": "commute_help_station_cross_section_acceptance_policy",
            "artifact_status": "complete_policy",
            "calibration_status": "not_calibrated",
            "policy_name": POLICY_NAME,
            "policy_version": POLICY_VERSION,
            "policy_algorithm_version": POLICY_ALGORITHM_VERSION,
            "source_station_cross_section_digest": source_manifest.content_digest,
            "source_station_cross_section_sha256": _sha256(source_path),
            "source_edge_map_sha256": source_manifest.edge_map_sha256,
            "source_relation_digest": relation_manifest.content_digest,
            "graph_version": source_manifest.graph_version,
            "sumo_network_version": source_manifest.sumo_network_version,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "decision_rules": rules,
            "policy_output": _output(policy_path, len(frame)),
            "review_output": _output(review_path, len(review)),
            "summary_output": _output(summary_path, None),
            "report_output": _output(report_path, None),
            "row_content_sha256": row_digest,
        }
        identity = {
            key: value
            for key, value in envelope.items()
            if key
            not in {
                "generated_at",
                "policy_output",
                "review_output",
                "summary_output",
                "report_output",
            }
        }
        identity["row_content_sha256"] = row_digest
        content_digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        manifest = StationCrossSectionPolicyManifestV1.model_validate(
            {**envelope, "content_digest": content_digest}
        )
        (pending / MANIFEST_JSON).write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        _promote_policy(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _build_summary(
    *,
    frame: pd.DataFrame,
    historical_directory: Path,
    quality_policy_directory: Path,
    relations: pd.DataFrame,
) -> dict[str, Any]:
    profiles = pq.read_table(
        historical_directory / "edge-time-distributions.parquet",
        columns=[
            "app_edge_id",
            "direction",
            "weekday",
            "bucket_start_minute",
            "source_station_ids_json",
        ],
    ).to_pandas()
    quality = pq.read_table(
        quality_policy_directory / "quality-policy-profile-status.parquet",
        columns=["app_edge_id", "weekday", "bucket_start_minute", "date_support_class"],
    ).to_pandas()
    direct = profiles.merge(
        quality,
        on=["app_edge_id", "weekday", "bucket_start_minute"],
        how="inner",
        validate="one_to_one",
    )
    direct = direct.loc[
        direct["date_support_class"].eq("direct_calibration_evidence")
    ].copy()
    accepted = set(frame.loc[frame["flow_measurement_eligible"], "station_id"])

    def canonical_station(value: object) -> str:
        text = str(value)
        return text if text.startswith("portal-station-") else f"portal-station-{text}"

    direct["all_stations_flow_eligible"] = direct["source_station_ids_json"].map(
        lambda raw: all(
            canonical_station(value) in accepted for value in json.loads(raw)
        )
    )
    context = relations[["app_edge_id", "road_name", "ref"]].drop_duplicates(
        "app_edge_id"
    )
    direct = direct.merge(context, on="app_edge_id", how="left", validate="many_to_one")
    corridor_rules = {
        "I-5": ("ref", r"(?:^|;)I 5(?:$|;)"),
        "I-205": ("ref", r"(?:^|;)I 205(?:$|;)"),
        "Interstate Bridge": ("road_name", r"Interstate ?Br(?:idge|dg)"),
        "Glenn L. Jackson Memorial Bridge": (
            "road_name",
            r"Glenn L\. Jackson Memorial Bridge",
        ),
        "Marquam Bridge": ("road_name", r"Marquam Bridge"),
        "OR-217": ("ref", r"(?:^|;)OR 217(?:$|;)"),
        "I-84 / US-30": ("ref", r"I 84|US 30"),
        "US-26": ("ref", r"(?:^|;)US 26(?:$|;)"),
    }
    corridor = {}
    for label, (column, pattern) in corridor_rules.items():
        subset = direct.loc[
            direct[column].fillna("").astype(str).str.contains(pattern, regex=True)
        ]
        corridor[label] = _coverage(subset)
    station_corridor = frame.merge(
        context, on="app_edge_id", how="left", suffixes=("", "_context")
    )
    bridge_outcomes = {}
    for label in (
        "Interstate Bridge",
        "Glenn L. Jackson Memorial Bridge",
        "Marquam Bridge",
    ):
        column, pattern = corridor_rules[label]
        source_match = (
            station_corridor[column]
            .fillna("")
            .astype(str)
            .str.contains(pattern, regex=True)
        )
        location_match = (
            station_corridor["location_text"]
            .fillna("")
            .astype(str)
            .str.contains(pattern, regex=True)
        )
        subset = station_corridor.loc[source_match | location_match]
        bridge_outcomes[label] = {
            "station_count": len(subset),
            "states": {
                str(key): int(value)
                for key, value in subset["policy_state"]
                .value_counts()
                .sort_index()
                .items()
            },
            "station_ids": sorted(subset["station_id"].tolist()),
        }
    states = {
        str(key): int(value)
        for key, value in frame["policy_state"].value_counts().sort_index().items()
    }
    return {
        "source_station_count": len(frame),
        "policy_state_counts": states,
        "accepted_station_count": int(frame["accepted_geometry"].sum()),
        "accepted_within_edge_count": states.get("accepted_within_edge", 0),
        "accepted_edge_transition_count": states.get("accepted_edge_transition", 0),
        "review_station_count": int(
            frame["policy_state"].str.startswith("review_").sum()
        ),
        "unmatched_station_count": states.get("unmatched_relation", 0),
        "flow_eligible_station_count": int(frame["flow_measurement_eligible"].sum()),
        "speed_eligible_station_count": int(frame["speed_measurement_eligible"].sum()),
        "acceptance_fraction": float(frame["accepted_geometry"].mean()),
        "human_review_row_count": int((~frame["accepted_geometry"]).sum()),
        "direct_profile_flow_coverage": {
            "total": len(direct),
            "comparator_v1_unique_one_edge": 52262,
            "phase_2_2a_topology_comparable": 111787,
            "phase_2_2d_flow_eligible": int(direct["all_stations_flow_eligible"].sum()),
            "still_review": int((~direct["all_stations_flow_eligible"]).sum()),
        },
        "direct_profile_flow_coverage_by_direction": {
            str(direction): _coverage(group)
            for direction, group in direct.groupby("direction", sort=True)
        },
        "corridor_direct_profile_flow_coverage": corridor,
        "bridge_station_outcomes": bridge_outcomes,
        "decision_variable_checks": {
            "all_source_station_mapping_confidence_high": bool(
                frame["station_app_mapping_confidence"].eq("high").all()
            ),
            "direction_incompatible_projectable_count": int(
                (frame["sumo_edge_id"].notna() & ~frame["direction_compatible"]).sum()
            ),
            "multiple_station_app_edge_count": int(
                frame.loc[frame["stations_on_app_edge"].gt(1), "app_edge_id"].nunique()
            ),
            "stations_on_multiple_station_edges": int(
                frame["stations_on_app_edge"].gt(1).sum()
            ),
        },
    }


def _decision(
    state: str,
    cross_section_type: str | None,
    accepted: bool,
    reason: str,
    *,
    transition_from: str | None = None,
    transition_to: str | None = None,
) -> dict[str, Any]:
    return {
        "policy_state": state,
        "cross_section_type": cross_section_type,
        "accepted_geometry": accepted,
        "flow_measurement_eligible": accepted,
        "speed_measurement_eligible": False,
        "decision_reason": reason,
        "transition_from_sumo_edge_id": transition_from,
        "transition_to_sumo_edge_id": transition_to,
    }


def _promote_policy(pending: Path, output: Path) -> None:
    required = {POLICY_PARQUET, REVIEW_PARQUET, SUMMARY_JSON, REPORT_MD, MANIFEST_JSON}
    present = {path.name for path in pending.iterdir()} if pending.is_dir() else set()
    if not required.issubset(present) or output.exists():
        raise StationCrossSectionPolicyError(
            "incomplete or conflicting policy artifact cannot promote"
        )
    os.replace(pending, output)


def _coverage(frame: pd.DataFrame) -> dict[str, int | float | None]:
    total = len(frame)
    eligible = int(frame["all_stations_flow_eligible"].sum()) if total else 0
    return {
        "total": total,
        "flow_eligible": eligible,
        "still_review": total - eligible,
        "flow_eligible_fraction": float(eligible / total) if total else None,
    }


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        normalized = {
            key: (None if pd.isna(value) else value) for key, value in row.items()
        }
        digest.update(canonical_json(normalized).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _output(path: Path, rows: int | None) -> PolicyOutputV1:
    return PolicyOutputV1(
        relative_path=path.name,
        sha256=_sha256(path),
        byte_count=path.stat().st_size,
        row_count=rows,
    )


def _render_report(summary: dict[str, Any]) -> str:
    states = summary["policy_state_counts"]
    coverage = summary["direct_profile_flow_coverage"]
    return "\n".join(
        [
            "# Phase 2.2d station cross-section acceptance policy",
            "",
            "Flow-location eligibility only. Point speed remains ineligible and nothing is historically calibrated.",
            "",
            f"- Accepted stations: {summary['accepted_station_count']:,} / {summary['source_station_count']:,}",
            f"- Accepted within-edge: {states.get('accepted_within_edge', 0):,}",
            f"- Accepted edge-transition: {states.get('accepted_edge_transition', 0):,}",
            f"- Review: {summary['review_station_count']:,}",
            f"- Unmatched: {summary['unmatched_station_count']:,}",
            f"- Direct profiles with all contributing stations flow-eligible: {coverage['phase_2_2d_flow_eligible']:,} / {coverage['total']:,}",
            "",
            "Rules: direction is a hard gate; lateral distance must be at most 25 m; a point within 5 m of an internal shared boundary in a proven ordered chain becomes an edge-transition cross-section; other points within 25 m of a member boundary remain review.",
            "",
            "The review Parquet is the bounded human-review package. Multiple stations remain independent.",
            "",
        ]
    )
