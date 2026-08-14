"""Resolve accepted app-edge to SUMO-edge relation topology for Phase 2.2a."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_policy import (
    HistoricalQualityPolicyManifestV1,
)
from backend.app.schemas.sumo_comparison_relations import (
    SUMO_COMPARISON_RELATION_ALGORITHM_VERSION,
    SUMO_COMPARISON_RELATION_PRODUCER_VERSION,
    SUMO_COMPARISON_RELATION_SCHEMA_VERSION,
    ComparisonRelationManifestV1,
    RelationClass,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

RELATION_DIRECTORY = "comparison-relations-v1"
RELATION_PARQUET = "app-sumo-comparison-relations.parquet"
RELATION_SUMMARY = "comparison-relation-summary.json"
RELATION_REPORT = "COMPARISON_RELATION_ANALYSIS.md"
RELATION_MANIFEST = "comparison-relation-manifest.json"


class ComparisonRelationError(RuntimeError):
    """The accepted mapping cannot satisfy the relation-resolution contract."""


@dataclass(frozen=True)
class SumoEdgeEvidence:
    edge_id: str
    from_junction: str
    to_junction: str
    length_m: float | None
    lane_count: int
    speed_limit_mps: float | None
    internal: bool


@dataclass(frozen=True)
class ChainAggregate:
    entrance_inflow_count: float
    entrance_flow_vph: float
    travel_time_seconds: float | None
    traversal_speed_kph: float | None
    reference_travel_time_seconds: float | None
    slowdown: float | None


def aggregate_ordered_chain_metrics(
    segments: list[dict[str, float | None]], *, interval_seconds: int = 900
) -> ChainAggregate:
    """Aggregate a proven chain without summing flow or averaging segment speeds."""
    if not segments or interval_seconds <= 0:
        raise ValueError("chain aggregation requires segments and a positive interval")
    first = segments[0]
    entered = _nonnegative(first.get("entered_count"), "entered_count")
    departed = _nonnegative(first.get("departed_count"), "departed_count")
    inflow = entered + departed
    flow = inflow * 3600.0 / interval_seconds

    lengths = [segment.get("length_m") for segment in segments]
    travel_times = [segment.get("travel_time_seconds") for segment in segments]
    speeds = [segment.get("speed_limit_mps") for segment in segments]
    observed = None
    traversal_speed = None
    reference = None
    slowdown = None
    if all(value is not None and float(value) >= 0 for value in lengths) and all(
        value is not None and float(value) > 0 for value in travel_times
    ):
        distance = sum(float(value) for value in lengths if value is not None)
        observed = sum(float(value) for value in travel_times if value is not None)
        traversal_speed = distance / observed * 3.6 if observed > 0 else None
    if all(value is not None and float(value) > 0 for value in speeds) and all(
        value is not None and float(value) >= 0 for value in lengths
    ):
        reference = sum(
            float(length) / float(speed)
            for length, speed in zip(lengths, speeds, strict=True)
            if length is not None and speed is not None
        )
    if observed is not None and observed > 0 and reference is not None:
        # Slowdown is observed traversal time divided by reference travel time.
        slowdown = observed / reference if reference > 0 else None
    return ChainAggregate(inflow, flow, observed, traversal_speed, reference, slowdown)


def classify_relation(
    rows: list[dict[str, Any]],
    edge_evidence: dict[str, SumoEdgeEvidence],
    external_incoming: dict[str, int] | None = None,
    external_outgoing: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Classify selected members from directed topology, never source row order."""
    external_incoming = external_incoming or {}
    external_outgoing = external_outgoing or {}
    duplicate_count = len(rows) - len({str(row["sumo_edge_id"]) for row in rows})
    edge_ids = sorted({str(row["sumo_edge_id"]) for row in rows})
    base = {
        "accepted_sumo_edge_count": len(edge_ids),
        "accepted_sumo_edge_ids": edge_ids,
        "ordered_sumo_edge_ids": [],
        "duplicate_relation_row_count": duplicate_count,
        "side_connection_count": 0,
        "topology_comparison_eligible": False,
        "primary_flow_comparison_eligible": False,
        "comparison_eligible": False,
        "exclusion_reason": None,
    }
    if duplicate_count:
        return _classification(base, "identity_conflict", "duplicate_accepted_relation_rows")
    identity_fields = ("graph_version", "sumo_network_version", "app_u", "app_v", "direction_signature")
    if any(len({str(row.get(field)) for row in rows}) != 1 for field in identity_fields):
        return _classification(base, "identity_conflict", "inconsistent_relation_identity")
    if any(edge_id not in edge_evidence for edge_id in edge_ids):
        return _classification(base, "unresolved", "missing_sumo_network_edge_evidence")
    if len(edge_ids) == 1:
        evidence = edge_evidence[edge_ids[0]]
        if evidence.internal:
            return _classification(base, "identity_conflict", "internal_sumo_edge_not_comparable")
        base.update(
            ordered_sumo_edge_ids=edge_ids,
            topology_comparison_eligible=True,
            primary_flow_comparison_eligible=True,
            comparison_eligible=True,
        )
        return _classification(base, "single_edge", None)

    edges = {edge_id: edge_evidence[edge_id] for edge_id in edge_ids}
    if any(edge.internal for edge in edges.values()):
        return _classification(base, "identity_conflict", "internal_sumo_member")
    if any(float(row.get("direction_error_degrees") or 0) > 90 for row in rows):
        return _classification(base, "direction_conflict", "accepted_member_reverses_app_direction")
    endpoint_pairs = [(edge.from_junction, edge.to_junction) for edge in edges.values()]
    if len(set(endpoint_pairs)) < len(endpoint_pairs):
        return _classification(base, "parallel_candidates", "members_share_directed_endpoints")

    successors = {
        edge_id: sorted(
            other_id
            for other_id, other in edges.items()
            if edge.to_junction == other.from_junction and other_id != edge_id
        )
        for edge_id, edge in edges.items()
    }
    predecessors = {
        edge_id: sorted(
            other_id
            for other_id, other in edges.items()
            if other.to_junction == edge.from_junction and other_id != edge_id
        )
        for edge_id, edge in edges.items()
    }
    components = _weak_components(edge_ids, successors, predecessors)
    if len(components) > 1:
        return _classification(base, "disconnected_candidates", "members_form_multiple_components")
    if any(len(successors[value]) > 1 or len(predecessors[value]) > 1 for value in edge_ids):
        return _classification(base, "branching_candidates", "member_topology_has_branch_or_merge")
    starts = [edge_id for edge_id in edge_ids if not predecessors[edge_id]]
    ends = [edge_id for edge_id in edge_ids if not successors[edge_id]]
    if not starts and not ends:
        return _classification(base, "cycle_candidates", "members_form_directed_cycle")
    if len(starts) != 1 or len(ends) != 1:
        return _classification(base, "unresolved", "directed_order_is_not_unique")
    ordered: list[str] = []
    cursor = starts[0]
    while cursor not in ordered:
        ordered.append(cursor)
        if not successors[cursor]:
            break
        cursor = successors[cursor][0]
    if len(ordered) != len(edge_ids) or ordered[-1] != ends[0]:
        return _classification(base, "unresolved", "directed_traversal_does_not_cover_members")

    app_u = str(rows[0]["app_u"])
    app_v = str(rows[0]["app_v"])
    chain_from = edges[ordered[0]].from_junction
    chain_to = edges[ordered[-1]].to_junction
    if chain_from == app_v and chain_to == app_u:
        return _classification(base, "direction_conflict", "chain_endpoints_reverse_app_edge")
    side_count = sum(external_outgoing.get(edge_id, 0) for edge_id in ordered[:-1])
    side_count += sum(external_incoming.get(edge_id, 0) for edge_id in ordered[1:])
    relation_class: RelationClass = (
        "ordered_chain_with_side_connections" if side_count else "ordered_linear_chain"
    )
    base.update(
        ordered_sumo_edge_ids=ordered,
        side_connection_count=side_count,
        topology_comparison_eligible=True,
        # No accepted station-to-SUMO-segment projection exists. Chain entrance flow
        # is retained as an approximation, not promoted to primary detector scoring.
        primary_flow_comparison_eligible=False,
        comparison_eligible=True,
        exclusion_reason="primary_flow_pending_detector_cross_section_alignment",
    )
    return _classification(base, relation_class, base["exclusion_reason"])


def resolve_comparison_relations(
    *,
    edge_map_path: Path,
    edge_map_report_path: Path,
    sumo_network_path: Path,
    network_manifest_path: Path,
    graph_edges_path: Path,
    historical_directory: Path,
    policy_directory: Path,
    station_mapping_path: Path,
    output_directory: Path,
) -> ComparisonRelationManifestV1:
    """Build the immutable derived resolution artifact atomically."""
    paths = [edge_map_path, edge_map_report_path, sumo_network_path, network_manifest_path, graph_edges_path, station_mapping_path]
    if not all(path.is_file() for path in paths):
        raise ComparisonRelationError("one or more required source artifacts are missing")
    if output_directory.exists():
        return ComparisonRelationManifestV1.model_validate_json(
            (output_directory / RELATION_MANIFEST).read_text(encoding="utf-8")
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)

    edge_map_sha = _sha256(edge_map_path)
    edge_map_report = json.loads(edge_map_report_path.read_text(encoding="utf-8"))
    report_sha = edge_map_report["artifacts"]["edge_map"]["sha256"]
    if report_sha != edge_map_sha:
        raise ComparisonRelationError("edge-map SHA does not match its report")
    network_manifest = json.loads(network_manifest_path.read_text(encoding="utf-8"))
    network_sha = _sha256(sumo_network_path)
    if network_manifest["artifact"]["sha256"] != network_sha:
        raise ComparisonRelationError("SUMO network SHA does not match its manifest")
    historical_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        (historical_directory / "historical-calibration-manifest.json").read_text(encoding="utf-8")
    )
    policy_manifest = HistoricalQualityPolicyManifestV1.model_validate_json(
        (policy_directory / "quality-policy-manifest.json").read_text(encoding="utf-8")
    )

    edge_map = pq.read_table(edge_map_path).to_pandas()
    accepted = edge_map.loc[edge_map["status"].eq("accepted")].copy()
    accepted_ids = set(accepted["sumo_edge_id"].astype(str))
    edge_to_apps: dict[str, set[str]] = defaultdict(set)
    members_by_app: dict[str, set[str]] = defaultdict(set)
    for app_edge_id, sumo_edge_id in accepted[["app_edge_id", "sumo_edge_id"]].itertuples(index=False):
        edge_to_apps[str(sumo_edge_id)].add(str(app_edge_id))
        members_by_app[str(app_edge_id)].add(str(sumo_edge_id))
    edge_evidence, external_in, external_out = _read_network_evidence(
        sumo_network_path, accepted_ids, edge_to_apps, members_by_app
    )
    graph = pq.read_table(
        graph_edges_path,
        columns=["edge_id", "road_name", "road_class", "ref", "length_m", "lanes", "maxspeed_kph"],
    ).to_pandas()
    graph = graph.rename(columns={"edge_id": "app_edge_id"})
    station = pd.read_csv(station_mapping_path, usecols=["station_id", "status", "edge_id", "longitude", "latitude"])
    accepted_station_counts = (
        station.loc[station["status"].eq("accepted")]
        .groupby("edge_id")["station_id"]
        .nunique()
        .to_dict()
    )

    records: list[dict[str, Any]] = []
    for app_edge_id, group in accepted.groupby("app_edge_id", sort=True):
        rows = group.to_dict(orient="records")
        incoming = {edge_id: external_in.get((str(app_edge_id), edge_id), 0) for edge_id in members_by_app[str(app_edge_id)]}
        outgoing = {edge_id: external_out.get((str(app_edge_id), edge_id), 0) for edge_id in members_by_app[str(app_edge_id)]}
        result = classify_relation(rows, edge_evidence, incoming, outgoing)
        ordered = result["ordered_sumo_edge_ids"]
        member_evidence = [edge_evidence[value] for value in result["accepted_sumo_edge_ids"] if value in edge_evidence]
        geometry_rows = group.drop_duplicates("sumo_edge_id").sort_values("sumo_edge_id")
        topology_evidence = [
            {
                "sumo_edge_id": value.edge_id,
                "from_junction": value.from_junction,
                "to_junction": value.to_junction,
                "length_m": value.length_m,
                "lane_count": value.lane_count,
                "speed_limit_mps": value.speed_limit_mps,
                "internal": value.internal,
            }
            for value in member_evidence
        ]
        record = {
            "schema_version": SUMO_COMPARISON_RELATION_SCHEMA_VERSION,
            "relation_id": _relation_id(str(app_edge_id), result["accepted_sumo_edge_ids"]),
            "app_edge_id": str(app_edge_id),
            "graph_version": str(group.iloc[0]["graph_version"]),
            "sumo_network_version": str(group.iloc[0]["sumo_network_version"]),
            "road_name": str(group.iloc[0]["road_name"]),
            "road_class": str(group.iloc[0]["road_class"]),
            "direction_signature": str(group.iloc[0]["direction_signature"]),
            "relation_class": result["relation_class"],
            "accepted_sumo_edge_count": result["accepted_sumo_edge_count"],
            "accepted_sumo_edge_ids_json": canonical_json(result["accepted_sumo_edge_ids"]),
            "ordered_sumo_edge_ids_json": canonical_json(ordered),
            "member_topology_evidence_json": canonical_json(topology_evidence),
            "app_geometry_sha256": _bytes_sha256(group.iloc[0]["app_geometry_wkb"]),
            "member_sumo_geometry_sha256_json": canonical_json(
                [
                    {
                        "sumo_edge_id": str(row.sumo_edge_id),
                        "geometry_sha256": _bytes_sha256(row.sumo_geometry_wkb),
                    }
                    for row in geometry_rows.itertuples(index=False)
                ]
            ),
            "member_sumo_osm_way_ids_json": canonical_json(
                [
                    {
                        "sumo_edge_id": str(row.sumo_edge_id),
                        "osm_way_ids": str(row.sumo_osm_way_ids),
                    }
                    for row in geometry_rows.itertuples(index=False)
                ]
            ),
            "mapping_match_methods_json": canonical_json(
                sorted({str(value) for value in group["match_method"]})
            ),
            "minimum_mapping_coverage_ratio": float(group["coverage_ratio"].min()),
            "minimum_mapping_match_score": float(group["match_score"].min()),
            "maximum_mapping_distance_m": float(group["match_distance_m"].max()),
            "maximum_direction_error_degrees": float(group["direction_error_degrees"].max()),
            "chain_from_junction": edge_evidence[ordered[0]].from_junction if ordered else None,
            "chain_to_junction": edge_evidence[ordered[-1]].to_junction if ordered else None,
            "total_sumo_length_m": _sum_optional([value.length_m for value in member_evidence]),
            "minimum_lane_count": min((value.lane_count for value in member_evidence), default=None),
            "maximum_lane_count": max((value.lane_count for value in member_evidence), default=None),
            "minimum_speed_limit_kph": _min_optional([_mps_to_kph(value.speed_limit_mps) for value in member_evidence]),
            "maximum_speed_limit_kph": _max_optional([_mps_to_kph(value.speed_limit_mps) for value in member_evidence]),
            "side_connection_count": result["side_connection_count"],
            "duplicate_relation_row_count": result["duplicate_relation_row_count"],
            "topology_comparison_eligible": result["topology_comparison_eligible"],
            "primary_flow_comparison_eligible": result["primary_flow_comparison_eligible"],
            "comparison_eligible": result["comparison_eligible"],
            "exclusion_reason": result["exclusion_reason"],
            "accepted_station_count": int(accepted_station_counts.get(str(app_edge_id), 0)),
            "detector_position_status": "station_points_not_projected_to_versioned_sumo_segment",
            "flow_semantics": "first_ordered_edge_entered_plus_departed_approximation",
            "speed_semantics": "total_distance_divided_by_sum_segment_travel_time",
            "travel_time_semantics": "sum_ordered_segment_travel_times",
            "slowdown_semantics": "observed_chain_travel_time_divided_by_reference_chain_travel_time",
            "original_edge_map_sha256": edge_map_sha,
            "resolution_algorithm_version": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION,
        }
        records.append(record)
    frame = pd.DataFrame(records).merge(graph, on="app_edge_id", how="left", validate="one_to_one", suffixes=("", "_graph"))
    frame = frame.sort_values("app_edge_id", kind="mergesort").reset_index(drop=True)
    policy = pq.read_table(
        policy_directory / "quality-policy-profile-status.parquet",
        columns=["app_edge_id", "direction", "date_support_class"],
    ).to_pandas()
    summary = _build_summary(frame, policy)
    report = _render_report(summary)

    pending.mkdir(parents=True)
    try:
        parquet_path = pending / RELATION_PARQUET
        summary_path = pending / RELATION_SUMMARY
        report_path = pending / RELATION_REPORT
        manifest_path = pending / RELATION_MANIFEST
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
            {
                b"schema_version": b"1",
                b"producer": b"app_sumo_comparison_relation_resolver",
                b"producer_version": SUMO_COMPARISON_RELATION_PRODUCER_VERSION.encode(),
                b"algorithm": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION.encode(),
            }
        )
        pq.write_table(table, parquet_path, compression="zstd", compression_level=9)
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        row_digest = _frame_digest(frame)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "commute_help_app_sumo_comparison_relation_resolution",
            "artifact_status": "complete_analysis",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC).isoformat(),
            "producer_name": "app_sumo_comparison_relation_resolver",
            "producer_version": SUMO_COMPARISON_RELATION_PRODUCER_VERSION,
            "resolution_algorithm_version": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION,
            "graph_version": historical_manifest.graph_version,
            "sumo_network_version": network_manifest["network_version"],
            "original_edge_map_sha256": edge_map_sha,
            "sumo_network_sha256": network_sha,
            "historical_profile_content_digest": historical_manifest.content_digest,
            "quality_policy_content_digest": policy_manifest.content_digest,
            "detector_location_semantics": "station_points_exist_but_no_versioned_station_to_sumo_segment_projection",
            "flow_semantics": "chain_entrance_entered_plus_departed_approximation_not_detector_aligned",
            "speed_semantics": "total_chain_distance_divided_by_sum_segment_travel_time",
            "slowdown_semantics": "sum_segment_observed_travel_time_divided_by_sum_segment_reference_travel_time",
            "relation_output": _file_identity(parquet_path, len(frame)),
            "summary_output": _file_identity(summary_path),
            "report_output": _file_identity(report_path),
            "row_content_sha256": row_digest,
        }
        payload["content_digest"] = _content_digest(payload)
        manifest = ComparisonRelationManifestV1.model_validate(payload)
        manifest_path.write_text(canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8")
        _promote(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _read_network_evidence(
    path: Path,
    selected: set[str],
    edge_to_apps: dict[str, set[str]],
    members_by_app: dict[str, set[str]],
) -> tuple[dict[str, SumoEdgeEvidence], dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    evidence: dict[str, SumoEdgeEvidence] = {}
    incoming: Counter[tuple[str, str]] = Counter()
    outgoing: Counter[tuple[str, str]] = Counter()
    for _event, element in ET.iterparse(path, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "edge":
            edge_id = element.attrib.get("id", "")
            if edge_id in selected:
                lanes = list(element.findall("lane"))
                lengths = [_float_or_none(lane.attrib.get("length")) for lane in lanes]
                speeds = [_float_or_none(lane.attrib.get("speed")) for lane in lanes]
                evidence[edge_id] = SumoEdgeEvidence(
                    edge_id=edge_id,
                    from_junction=element.attrib.get("from", ""),
                    to_junction=element.attrib.get("to", ""),
                    length_m=_median_optional(lengths),
                    lane_count=len(lanes),
                    speed_limit_mps=_median_optional(speeds),
                    internal=edge_id.startswith(":") or element.attrib.get("function") == "internal",
                )
            element.clear()
        elif tag == "connection":
            source = element.attrib.get("from", "")
            target = element.attrib.get("to", "")
            source_apps = edge_to_apps.get(source, set())
            target_apps = edge_to_apps.get(target, set())
            for app_edge_id in source_apps:
                if target not in members_by_app[app_edge_id]:
                    outgoing[(app_edge_id, source)] += 1
            for app_edge_id in target_apps:
                if source not in members_by_app[app_edge_id]:
                    incoming[(app_edge_id, target)] += 1
            element.clear()
    return evidence, dict(incoming), dict(outgoing)


def _build_summary(frame: pd.DataFrame, policy: pd.DataFrame) -> dict[str, Any]:
    multi = frame.loc[frame["accepted_sumo_edge_count"].gt(1)]
    direct = policy.loc[policy["date_support_class"].eq("direct_calibration_evidence")]
    joined = direct.merge(
        frame[["app_edge_id", "relation_class", "accepted_sumo_edge_count", "comparison_eligible", "primary_flow_comparison_eligible", "road_name", "road_class", "ref"]],
        on="app_edge_id",
        how="left",
        validate="many_to_one",
    )
    joined["relation_class"] = joined["relation_class"].fillna("review_or_absent")
    current = int(joined["relation_class"].eq("single_edge").sum())
    chain = int(joined["relation_class"].isin(["ordered_linear_chain", "ordered_chain_with_side_connections"]).sum())
    review = int(joined["relation_class"].eq("review_or_absent").sum())
    ambiguity = len(joined) - current - chain - review
    corridors = {}
    corridor_rules = {
        "I-5": r"(?:^|;)I 5(?:$|;)", "I-205": r"(?:^|;)I 205(?:$|;)",
        "Interstate Bridge": r"Interstate Bridge", "Glenn L. Jackson Memorial Bridge": r"Glenn L\. Jackson Memorial Bridge",
        "Marquam Bridge": r"Marquam Bridge", "OR-217": r"(?:^|;)OR 217(?:$|;)",
        "I-84 / US-30": r"I 84|US 30", "US-26": r"(?:^|;)US 26(?:$|;)",
    }
    ref = joined["ref"].fillna("").astype(str)
    name = joined["road_name"].fillna("").astype(str)
    for label, pattern in corridor_rules.items():
        source = name if "Bridge" in label else ref
        subset = joined.loc[source.str.contains(pattern, regex=True, na=False)]
        corridors[label] = _coverage_counts(subset)
    return {
        "artifact_status": "complete_analysis",
        "calibration_status": "not_calibrated",
        "one_to_many_app_edge_count": len(multi),
        "one_to_many_accepted_relation_count": int(multi["accepted_sumo_edge_count"].sum()),
        "topology_class_app_edge_counts": {str(key): int(value) for key, value in multi["relation_class"].value_counts().sort_index().items()},
        "cardinality_app_edge_counts": {str(key): int(value) for key, value in multi["accepted_sumo_edge_count"].value_counts().sort_index().items()},
        "maximum_accepted_sumo_edge_count": int(multi["accepted_sumo_edge_count"].max()),
        "proven_ordered_chain_app_edge_count": int(multi["comparison_eligible"].sum()),
        "true_ambiguity_app_edge_count": int(multi["relation_class"].isin(["parallel_candidates", "branching_candidates", "disconnected_candidates", "cycle_candidates", "direction_conflict", "identity_conflict"]).sum()),
        "unresolved_app_edge_count": int(multi["relation_class"].eq("unresolved").sum()),
        "direct_profile_coverage": {
            "total": len(joined), "current_unique_one_edge_comparable": current,
            "ordered_chain_topology_comparable": chain, "remaining_ambiguity": ambiguity,
            "review_or_absent": review, "hypothetical_total_topology_comparable": current + chain,
            "hypothetical_topology_comparable_fraction": (current + chain) / len(joined),
            "primary_flow_comparable_after_resolution": int(joined["primary_flow_comparison_eligible"].fillna(False).sum()),
        },
        "direct_profile_coverage_by_relation_class": {str(key): int(value) for key, value in joined["relation_class"].value_counts().sort_index().items()},
        "direct_profile_coverage_by_segment_count": {str(key): int(value) for key, value in joined["accepted_sumo_edge_count"].fillna(0).astype(int).value_counts().sort_index().items()},
        "direct_profile_coverage_by_direction": {
            str(direction): _coverage_counts(group)
            for direction, group in joined.groupby("direction", sort=True)
        },
        "corridor_direct_profile_coverage": corridors,
        "semantics": {
            "flow": "first ordered edge entered+departed; approximation only until detector cross-section is versioned",
            "speed": "total chain distance / sum segment travel times; null if any required segment evidence is missing",
            "travel_time": "sum ordered segment travel times",
            "slowdown": "observed chain travel time / sum(length / authoritative segment speed limit); never average ratios",
            "detector_location": "accepted station points exist, but no versioned station-to-SUMO-segment projection exists",
        },
    }


def _coverage_counts(frame: pd.DataFrame) -> dict[str, int | float | None]:
    current = int(frame["relation_class"].eq("single_edge").sum())
    chains = int(frame["relation_class"].isin(["ordered_linear_chain", "ordered_chain_with_side_connections"]).sum())
    return {"total": len(frame), "current_unique": current, "ordered_chain": chains, "hypothetical_total": current + chains, "hypothetical_fraction": (current + chains) / len(frame) if len(frame) else None}


def _render_report(summary: dict[str, Any]) -> str:
    coverage = summary["direct_profile_coverage"]
    lines = [
        "# Phase 2.2a accepted one-to-many comparison relations", "",
        "This is topology and semantics analysis only. It does not alter the edge map, comparator-v1, telemetry, or calibration state.", "",
        "## Result", "", f"- One-to-many app edges: {summary['one_to_many_app_edge_count']:,}",
        f"- Proven ordered chains: {summary['proven_ordered_chain_app_edge_count']:,}",
        f"- True topology ambiguities/conflicts: {summary['true_ambiguity_app_edge_count']:,}", f"- Unresolved: {summary['unresolved_app_edge_count']:,}", "",
        "## Direct historical profile coverage", "", f"- Existing one-edge comparable: {coverage['current_unique_one_edge_comparable']:,}",
        f"- Additional topologically comparable chains: {coverage['ordered_chain_topology_comparable']:,}",
        f"- Hypothetical topology coverage: {coverage['hypothetical_total_topology_comparable']:,} / {coverage['total']:,} ({coverage['hypothetical_topology_comparable_fraction']:.2%})", "",
        "## Semantic boundary", "", "Ordered-chain speed and travel time can use distance divided by summed segment travel time. Flow must never be summed across sequential members. The first member's `entered + departed` is a chain-entrance approximation, but no accepted station-to-SUMO-segment projection currently establishes that the PORTAL detector sits at that entrance. Consequently ordered chains are topology-eligible while primary detector-flow scoring remains pending cross-section alignment.", "",
        "See `comparison-relation-summary.json` for class, cardinality, segment-count, direction, and corridor tables.",
    ]
    return "\n".join(lines) + "\n"


def _weak_components(edge_ids: list[str], successors: dict[str, list[str]], predecessors: dict[str, list[str]]) -> list[set[str]]:
    remaining = set(edge_ids)
    components = []
    while remaining:
        start = min(remaining)
        component = set()
        queue = deque([start])
        while queue:
            value = queue.popleft()
            if value in component:
                continue
            component.add(value)
            queue.extend(successors[value])
            queue.extend(predecessors[value])
        remaining -= component
        components.append(component)
    return components


def _classification(base: dict[str, Any], relation_class: RelationClass, reason: str | None) -> dict[str, Any]:
    return {**base, "relation_class": relation_class, "exclusion_reason": reason}


def _nonnegative(value: float | None, name: str) -> float:
    if value is None or float(value) < 0:
        raise ValueError(f"{name} must be nonnegative")
    return float(value)


def _float_or_none(value: str | None) -> float | None:
    return None if value is None else float(value)


def _median_optional(values: list[float | None]) -> float | None:
    present = sorted(float(value) for value in values if value is not None)
    if not present:
        return None
    middle = len(present) // 2
    return present[middle] if len(present) % 2 else (present[middle - 1] + present[middle]) / 2


def _sum_optional(values: list[float | None]) -> float | None:
    return None if not values or any(value is None for value in values) else sum(float(value) for value in values if value is not None)


def _min_optional(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _max_optional(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _mps_to_kph(value: float | None) -> float | None:
    return None if value is None else value * 3.6


def _relation_id(app_edge_id: str, edge_ids: list[str]) -> str:
    return hashlib.sha256(canonical_json({"app_edge_id": app_edge_id, "accepted_sumo_edge_ids": edge_ids, "algorithm": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION}).encode()).hexdigest()


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        digest.update(canonical_json({key: _json_value(value) for key, value in row.items()}).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return hashlib.sha256(bytes(value)).hexdigest()


def _file_identity(path: Path, row_count: int | None = None) -> dict[str, Any]:
    return {"relative_path": path.name, "sha256": _sha256(path), "byte_count": path.stat().st_size, "row_count": row_count}


def _content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _promote(pending: Path, output: Path) -> None:
    manifest_path = pending / RELATION_MANIFEST
    if output.exists() or not manifest_path.is_file():
        raise ComparisonRelationError("partial or conflicting relation artifact cannot promote")
    ComparisonRelationManifestV1.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    os.replace(pending, output)
