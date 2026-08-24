"""Build comparison-relations-v2 by repairing proven synthetic SUMO boundaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.sumo_comparison_relations import (
    ComparisonRelationManifestV1,
    ComparisonRelationManifestV2,
    SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2,
    SUMO_COMPARISON_RELATION_PRODUCER_VERSION_V2,
    SUMO_COMPARISON_RELATION_REPAIR_REASON_V2,
    SUMO_COMPARISON_RELATION_SCHEMA_VERSION_V2,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.comparison_relation_service import (
    ComparisonRelationError,
    _content_digest,
    _file_identity,
    _frame_digest,
    _relation_id,
    _sha256,
)


RELATION_PARQUET = "app-sumo-comparison-relations.parquet"
RELATION_SUMMARY = "comparison-relation-summary.json"
RELATION_REPORT = "COMPARISON_RELATION_ANALYSIS.md"
RELATION_MANIFEST = "comparison-relation-manifest.json"
EXPECTED_DIAGNOSTIC_ID = (
    "HIST-MOSS-R3-STATIC-TRANSITION-DISAGREEMENTS-DIAGNOSTIC-V2"
)
BOUNDARY_CLASSIFICATION = (
    "D_COMPARISON_RELATION_CHAIN_BOUNDARY_OMITS_SYNTHETIC_EXTERNAL_SUMO_SEGMENT"
)
CLUSTERING_CLASSIFICATION = (
    "H_SUMO_JUNCTION_CLUSTERING_COLLAPSES_DISTINCT_APP_ENDPOINT_NODES"
)


@dataclass(frozen=True)
class NetworkEdge:
    edge_id: str
    from_junction: str
    to_junction: str
    type_id: str
    length_m: float | None
    lane_count: int
    speed_limit_mps: float | None
    orig_ids: tuple[str, ...]
    internal: bool
    synthetic_boundary_kind: str | None


def build_comparison_relations_v2(
    *,
    parent_directory: Path,
    edge_map_path: Path,
    sumo_network_path: Path,
    network_manifest_path: Path,
    graph_edges_path: Path,
    diagnostic_summary_path: Path,
    diagnostic_provenance_path: Path,
    output_directory: Path,
) -> ComparisonRelationManifestV2:
    """Regenerate a versioned relation artifact from frozen structural lineage."""
    required = (
        parent_directory / RELATION_MANIFEST,
        edge_map_path,
        sumo_network_path,
        network_manifest_path,
        graph_edges_path,
        diagnostic_summary_path,
        diagnostic_provenance_path,
    )
    if not all(path.is_file() for path in required):
        raise ComparisonRelationError("one or more v2 boundary-repair inputs are missing")
    if output_directory.exists():
        return ComparisonRelationManifestV2.model_validate_json(
            (output_directory / RELATION_MANIFEST).read_text(encoding="utf-8")
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        raise ComparisonRelationError(f"refusing existing pending artifact: {pending}")

    parent_manifest_path = parent_directory / RELATION_MANIFEST
    parent_manifest = ComparisonRelationManifestV1.model_validate_json(
        parent_manifest_path.read_text(encoding="utf-8")
    )
    parent_parquet = parent_directory / parent_manifest.relation_output.relative_path
    if _sha256(parent_parquet) != parent_manifest.relation_output.sha256:
        raise ComparisonRelationError("parent relation Parquet SHA-256 mismatch")
    edge_map_sha = _sha256(edge_map_path)
    if edge_map_sha != parent_manifest.original_edge_map_sha256:
        raise ComparisonRelationError("edge-map SHA-256 differs from parent lineage")
    network_manifest = json.loads(network_manifest_path.read_text(encoding="utf-8"))
    network_sha = _sha256(sumo_network_path)
    if network_manifest["artifact"]["sha256"] != network_sha:
        raise ComparisonRelationError("SUMO network SHA-256 differs from manifest")
    if parent_manifest.sumo_network_sha256 != network_sha:
        raise ComparisonRelationError("SUMO network SHA-256 differs from parent lineage")

    diagnostic = json.loads(diagnostic_summary_path.read_text(encoding="utf-8"))
    diagnostic_provenance = json.loads(
        diagnostic_provenance_path.read_text(encoding="utf-8")
    )
    _require_diagnostic_contract(diagnostic, diagnostic_provenance)

    parent = pq.read_table(parent_parquet).to_pandas()
    parent["app_edge_id"] = parent["app_edge_id"].astype(str)
    if not parent["app_edge_id"].is_unique:
        raise ComparisonRelationError("parent relation app-edge identities are not unique")
    graph = pq.read_table(
        graph_edges_path, columns=["edge_id", "u", "v", "road_class"]
    ).to_pandas()
    graph["edge_id"] = graph["edge_id"].astype(str)
    graph_by_id = graph.set_index("edge_id").to_dict(orient="index")

    edge_map = pq.read_table(
        edge_map_path,
        columns=[
            "app_edge_id",
            "sumo_edge_id",
            "status",
            "app_osm_way_ids",
            "sumo_osm_way_ids",
        ],
    ).to_pandas()
    accepted = edge_map.loc[edge_map["status"].eq("accepted")].copy()
    accepted["app_edge_id"] = accepted["app_edge_id"].astype(str)
    accepted["sumo_edge_id"] = accepted["sumo_edge_id"].astype(str)
    accepted_owners: dict[str, set[str]] = defaultdict(set)
    app_orig_ids: dict[str, set[str]] = defaultdict(set)
    for row in accepted.to_dict(orient="records"):
        app_edge_id = str(row["app_edge_id"])
        sumo_edge_id = str(row["sumo_edge_id"])
        accepted_owners[sumo_edge_id].add(app_edge_id)
        app_orig_ids[app_edge_id].update(_parse_json_strings(row["app_osm_way_ids"]))

    network_edges, incoming, outgoing, connection_counts = _read_network(
        sumo_network_path
    )
    proposals: dict[str, dict[str, Any]] = {}
    candidate_owners: dict[str, list[str]] = defaultdict(list)
    for row in parent.sort_values("app_edge_id", kind="mergesort").to_dict(
        orient="records"
    ):
        app_edge_id = str(row["app_edge_id"])
        ordered = _parse_json_strings(row["ordered_sumo_edge_ids_json"])
        graph_row = graph_by_id.get(app_edge_id)
        if (
            graph_row is None
            or str(row.get("road_class")) != "motorway"
            or str(graph_row.get("road_class")) != "motorway"
            or not bool(row.get("topology_comparison_eligible"))
            or not bool(row.get("comparison_eligible"))
            or not ordered
        ):
            proposals[app_edge_id] = _proposal(row, ordered, [], [])
            continue
        prepend = _boundary_candidates(
            boundary="APP_U_PREPEND",
            app_edge_id=app_edge_id,
            app_boundary=str(graph_row["u"]),
            boundary_member_id=ordered[0],
            candidate_ids=incoming.get(ordered[0], set()),
            app_orig_ids=app_orig_ids[app_edge_id],
            accepted_owners=accepted_owners,
            network_edges=network_edges,
            ordered_members=set(ordered),
        )
        append = _boundary_candidates(
            boundary="APP_V_APPEND",
            app_edge_id=app_edge_id,
            app_boundary=str(graph_row["v"]),
            boundary_member_id=ordered[-1],
            candidate_ids=outgoing.get(ordered[-1], set()),
            app_orig_ids=app_orig_ids[app_edge_id],
            accepted_owners=accepted_owners,
            network_edges=network_edges,
            ordered_members=set(ordered),
        )
        if len(prepend) > 1 or len(append) > 1:
            raise ComparisonRelationError(
                f"ambiguous synthetic boundary ownership for app edge {app_edge_id}"
            )
        evidence = prepend + append
        added = [item["synthetic_sumo_edge_id"] for item in evidence]
        augmented = (
            ([prepend[0]["synthetic_sumo_edge_id"]] if prepend else [])
            + ordered
            + ([append[0]["synthetic_sumo_edge_id"]] if append else [])
        )
        proposals[app_edge_id] = _proposal(row, augmented, added, evidence)
        for edge_id in added:
            candidate_owners[edge_id].append(app_edge_id)

    duplicate_owners = {
        edge_id: owners
        for edge_id, owners in candidate_owners.items()
        if len(set(owners)) != 1
    }
    if duplicate_owners:
        raise ComparisonRelationError(
            f"synthetic boundary edge has non-unique relation ownership: {duplicate_owners}"
        )

    records = []
    changed = []
    for parent_row in parent.sort_values("app_edge_id", kind="mergesort").to_dict(
        orient="records"
    ):
        app_edge_id = str(parent_row["app_edge_id"])
        proposal = proposals[app_edge_id]
        if proposal["added"]:
            updated = _updated_relation(
                parent_row,
                proposal,
                network_edges,
                connection_counts,
            )
            changed.append(
                {
                    "app_edge_id": app_edge_id,
                    "parent_relation_id": str(parent_row["relation_id"]),
                    "relation_id": str(updated["relation_id"]),
                    "parent_ordered_sumo_edge_ids": _parse_json_strings(
                        parent_row["ordered_sumo_edge_ids_json"]
                    ),
                    "ordered_sumo_edge_ids": proposal["ordered"],
                    "added_synthetic_boundary_member_ids": proposal["added"],
                    "ownership_evidence": proposal["evidence"],
                    "parent_total_sumo_length_m": parent_row["total_sumo_length_m"],
                    "total_sumo_length_m": updated["total_sumo_length_m"],
                }
            )
        else:
            updated = dict(parent_row)
        updated.update(
            {
                "schema_version": SUMO_COMPARISON_RELATION_SCHEMA_VERSION_V2,
                "parent_relation_id": str(parent_row["relation_id"]),
                "boundary_repair_applied": bool(proposal["added"]),
                "synthetic_boundary_member_ids_json": canonical_json(
                    proposal["added"]
                ),
                "synthetic_boundary_ownership_evidence_json": canonical_json(
                    proposal["evidence"]
                ),
                "resolution_algorithm_version": (
                    SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2
                ),
            }
        )
        records.append(updated)

    if len(changed) < 3:
        raise ComparisonRelationError(
            "class-level repair did not recover the three diagnostic witnesses"
        )
    frame = pd.DataFrame(records).sort_values(
        "app_edge_id", kind="mergesort"
    ).reset_index(drop=True)
    summary = {
        "artifact_status": "complete_analysis",
        "repair_reason": SUMO_COMPARISON_RELATION_REPAIR_REASON_V2,
        "parent_relation_count": len(parent),
        "relation_count": len(frame),
        "changed_relation_count": len(changed),
        "synthetic_boundary_member_count": sum(
            len(item["added_synthetic_boundary_member_ids"]) for item in changed
        ),
        "changed_relations": changed,
        "traffic_values_loaded": False,
        "development_loaded": False,
        "blind_loaded": False,
    }
    report = _render_report(summary)

    pending.mkdir(parents=True)
    try:
        parquet_path = pending / RELATION_PARQUET
        summary_path = pending / RELATION_SUMMARY
        report_path = pending / RELATION_REPORT
        manifest_path = pending / RELATION_MANIFEST
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
            {
                b"schema_version": b"2",
                b"producer": b"app_sumo_comparison_relation_boundary_repair",
                b"producer_version": SUMO_COMPARISON_RELATION_PRODUCER_VERSION_V2.encode(),
                b"algorithm": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2.encode(),
            }
        )
        pq.write_table(table, parquet_path, compression="zstd", compression_level=9)
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        payload: dict[str, Any] = {
            "schema_version": SUMO_COMPARISON_RELATION_SCHEMA_VERSION_V2,
            "artifact_type": "commute_help_app_sumo_comparison_relation_resolution",
            "artifact_status": "complete_analysis",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC).isoformat(),
            "producer_name": "app_sumo_comparison_relation_boundary_repair",
            "producer_version": SUMO_COMPARISON_RELATION_PRODUCER_VERSION_V2,
            "resolution_algorithm_version": SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2,
            "repair_reason": SUMO_COMPARISON_RELATION_REPAIR_REASON_V2,
            "graph_version": parent_manifest.graph_version,
            "sumo_network_version": parent_manifest.sumo_network_version,
            "original_edge_map_sha256": edge_map_sha,
            "graph_edges_sha256": _sha256(graph_edges_path),
            "sumo_network_sha256": network_sha,
            "parent_relation_manifest_sha256": _sha256(parent_manifest_path),
            "parent_relation_content_digest": parent_manifest.content_digest,
            "parent_relation_parquet_sha256": parent_manifest.relation_output.sha256,
            "repair_evidence_summary_sha256": _sha256(diagnostic_summary_path),
            "repair_evidence_provenance_sha256": _sha256(
                diagnostic_provenance_path
            ),
            "changed_relation_count": len(changed),
            "synthetic_boundary_member_count": summary[
                "synthetic_boundary_member_count"
            ],
            "relation_output": _file_identity(parquet_path, len(frame)),
            "summary_output": _file_identity(summary_path),
            "report_output": _file_identity(report_path),
            "row_content_sha256": _frame_digest(frame),
        }
        payload["content_digest"] = _content_digest(payload)
        manifest = ComparisonRelationManifestV2.model_validate(payload)
        manifest_path.write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )
        _promote_v2(pending, output_directory)
        return manifest
    except BaseException:
        # Preserve the pending directory for forensic inspection; no frozen evidence is deleted.
        raise


def _proposal(
    row: dict[str, Any],
    ordered: list[str],
    added: list[str],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "parent_relation_id": str(row["relation_id"]),
        "ordered": ordered,
        "added": added,
        "evidence": evidence,
    }


def _boundary_candidates(
    *,
    boundary: str,
    app_edge_id: str,
    app_boundary: str,
    boundary_member_id: str,
    candidate_ids: set[str],
    app_orig_ids: set[str],
    accepted_owners: dict[str, set[str]],
    network_edges: dict[str, NetworkEdge],
    ordered_members: set[str],
) -> list[dict[str, Any]]:
    boundary_member = network_edges.get(boundary_member_id)
    if boundary_member is None:
        raise ComparisonRelationError(
            f"network evidence missing for relation boundary {boundary_member_id}"
        )
    output = []
    for edge_id in sorted(candidate_ids):
        candidate = network_edges.get(edge_id)
        if candidate is None or candidate.internal or edge_id in ordered_members:
            continue
        if candidate.synthetic_boundary_kind is None:
            continue
        if candidate.type_id != "highway.motorway":
            continue
        if accepted_owners.get(edge_id):
            continue
        candidate_orig_ids = set(candidate.orig_ids)
        boundary_orig_ids = set(boundary_member.orig_ids)
        if (
            not candidate_orig_ids
            or not candidate_orig_ids.issubset(app_orig_ids)
            or not candidate_orig_ids.intersection(boundary_orig_ids)
        ):
            continue
        if boundary == "APP_U_PREPEND":
            endpoints_match = (
                candidate.from_junction == app_boundary
                and candidate.to_junction == boundary_member.from_junction
            )
        else:
            endpoints_match = (
                boundary_member.to_junction == candidate.from_junction
                and candidate.to_junction == app_boundary
            )
        if not endpoints_match:
            continue
        output.append(
            {
                "ownership_rule": (
                    "UNOWNED_NETCONVERT_SYNTHETIC_EXTERNAL_MOTORWAY_BOUNDARY_"
                    "WITH_MATCHING_ORIGID_DIRECTED_CONNECTION_AND_APP_ENDPOINT"
                ),
                "boundary": boundary,
                "app_edge_id": app_edge_id,
                "app_boundary_junction": app_boundary,
                "boundary_member_id": boundary_member_id,
                "boundary_member_orig_ids": sorted(boundary_orig_ids),
                "synthetic_sumo_edge_id": edge_id,
                "synthetic_boundary_kind": candidate.synthetic_boundary_kind,
                "synthetic_from_junction": candidate.from_junction,
                "synthetic_to_junction": candidate.to_junction,
                "synthetic_type": candidate.type_id,
                "synthetic_orig_ids": sorted(candidate_orig_ids),
                "accepted_edge_map_owner_count": 0,
                "directed_connection_proven": True,
            }
        )
    return output


def _updated_relation(
    parent: dict[str, Any],
    proposal: dict[str, Any],
    network_edges: dict[str, NetworkEdge],
    connection_counts: Counter[tuple[str, str]],
) -> dict[str, Any]:
    ordered = proposal["ordered"]
    member_set = set(ordered)
    evidence = [network_edges[edge_id] for edge_id in ordered]
    if any(edge.internal for edge in evidence):
        raise ComparisonRelationError("internal edge entered external comparison chain")
    for source, target in zip(ordered[:-1], ordered[1:], strict=True):
        if connection_counts[(source, target)] <= 0:
            raise ComparisonRelationError(
                f"augmented relation is not statically contiguous: {source} -> {target}"
            )
    side_count = 0
    for source in ordered[:-1]:
        side_count += sum(
            count
            for (candidate_source, target), count in connection_counts.items()
            if candidate_source == source and target not in member_set
        )
    for target in ordered[1:]:
        side_count += sum(
            count
            for (source, candidate_target), count in connection_counts.items()
            if candidate_target == target and source not in member_set
        )
    geometry_rows = {
        str(item["sumo_edge_id"]): item
        for item in json.loads(str(parent["member_sumo_geometry_sha256_json"]))
    }
    methods = set(json.loads(str(parent["mapping_match_methods_json"])))
    methods.add("synthetic_boundary_orig_id_continuation_v2")
    updated = dict(parent)
    accepted_ids = sorted(member_set)
    updated.update(
        {
            "relation_id": _relation_id(str(parent["app_edge_id"]), accepted_ids),
            "relation_class": (
                "ordered_chain_with_side_connections"
                if side_count
                else "ordered_linear_chain"
            ),
            "accepted_sumo_edge_count": len(accepted_ids),
            "accepted_sumo_edge_ids_json": canonical_json(accepted_ids),
            "ordered_sumo_edge_ids_json": canonical_json(ordered),
            "member_topology_evidence_json": canonical_json(
                [_topology_row(network_edges[edge_id]) for edge_id in accepted_ids]
            ),
            "member_sumo_geometry_sha256_json": canonical_json(
                [
                    geometry_rows.get(
                        edge_id,
                        {"sumo_edge_id": edge_id, "geometry_sha256": None},
                    )
                    for edge_id in accepted_ids
                ]
            ),
            "member_sumo_osm_way_ids_json": canonical_json(
                [
                    {
                        "sumo_edge_id": edge_id,
                        "osm_way_ids": canonical_json(
                            list(network_edges[edge_id].orig_ids)
                        ),
                    }
                    for edge_id in accepted_ids
                ]
            ),
            "mapping_match_methods_json": canonical_json(sorted(methods)),
            "chain_from_junction": evidence[0].from_junction,
            "chain_to_junction": evidence[-1].to_junction,
            "total_sumo_length_m": _sum_required(
                [edge.length_m for edge in evidence]
            ),
            "minimum_lane_count": min(edge.lane_count for edge in evidence),
            "maximum_lane_count": max(edge.lane_count for edge in evidence),
            "minimum_speed_limit_kph": min(
                float(edge.speed_limit_mps) * 3.6
                for edge in evidence
                if edge.speed_limit_mps is not None
            ),
            "maximum_speed_limit_kph": max(
                float(edge.speed_limit_mps) * 3.6
                for edge in evidence
                if edge.speed_limit_mps is not None
            ),
            "side_connection_count": side_count,
        }
    )
    return updated


def _read_network(
    path: Path,
) -> tuple[
    dict[str, NetworkEdge],
    dict[str, set[str]],
    dict[str, set[str]],
    Counter[tuple[str, str]],
]:
    edges: dict[str, NetworkEdge] = {}
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    connection_counts: Counter[tuple[str, str]] = Counter()
    for _event, element in ET.iterparse(path, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "edge":
            edge_id = element.attrib.get("id", "")
            lanes = list(element.findall("lane"))
            lengths = [_optional_float(lane.attrib.get("length")) for lane in lanes]
            speeds = [_optional_float(lane.attrib.get("speed")) for lane in lanes]
            orig_ids = sorted(
                {
                    value
                    for lane in lanes
                    for param in lane.findall("param")
                    if param.attrib.get("key") == "origId"
                    for value in param.attrib.get("value", "").split()
                    if value
                }
            )
            internal = edge_id.startswith(":") or element.attrib.get("function") == "internal"
            edges[edge_id] = NetworkEdge(
                edge_id=edge_id,
                from_junction=element.attrib.get("from", ""),
                to_junction=element.attrib.get("to", ""),
                type_id=element.attrib.get("type", ""),
                length_m=_median(lengths),
                lane_count=len(lanes),
                speed_limit_mps=_median(speeds),
                orig_ids=tuple(orig_ids),
                internal=internal,
                synthetic_boundary_kind=_synthetic_boundary_kind(
                    edge_id,
                    element.attrib.get("from", ""),
                    element.attrib.get("to", ""),
                ),
            )
            element.clear()
        elif tag == "connection":
            source = element.attrib.get("from", "")
            target = element.attrib.get("to", "")
            if source and target and not source.startswith(":") and not target.startswith(":"):
                outgoing[source].add(target)
                incoming[target].add(source)
                connection_counts[(source, target)] += 1
            element.clear()
    return edges, dict(incoming), dict(outgoing), connection_counts


def _synthetic_boundary_kind(edge_id: str, source: str, target: str) -> str | None:
    if edge_id.endswith("-AddedOnRampEdge") and target.endswith("-AddedOnRampNode"):
        return "NETCONVERT_ADDED_ON_RAMP_EXTERNAL_CONTINUATION"
    if edge_id.endswith("-AddedOffRampEdge") and source.endswith("-AddedOffRampNode"):
        return "NETCONVERT_ADDED_OFF_RAMP_EXTERNAL_CONTINUATION"
    return None


def _require_diagnostic_contract(
    summary: dict[str, Any], provenance: dict[str, Any]
) -> None:
    if summary.get("diagnostic_id") != EXPECTED_DIAGNOSTIC_ID:
        raise ComparisonRelationError("unexpected boundary-repair diagnostic identity")
    if summary.get("status") != "PASS_TRAFFIC_BLIND_STATIC_DIAGNOSIS":
        raise ComparisonRelationError("boundary-repair diagnostic did not pass")
    counts = summary.get("aggregate_classification", {}).get(
        "classification_counts", {}
    )
    if counts.get(BOUNDARY_CLASSIFICATION) != 3:
        raise ComparisonRelationError("diagnostic does not contain three boundary defects")
    if counts.get(CLUSTERING_CLASSIFICATION) != 1:
        raise ComparisonRelationError("diagnostic clustering-case count drift")
    if provenance.get("diagnostic_id") != EXPECTED_DIAGNOSTIC_ID:
        raise ComparisonRelationError("diagnostic provenance identity drift")


def _topology_row(edge: NetworkEdge) -> dict[str, Any]:
    return {
        "sumo_edge_id": edge.edge_id,
        "from_junction": edge.from_junction,
        "to_junction": edge.to_junction,
        "length_m": edge.length_m,
        "lane_count": edge.lane_count,
        "speed_limit_mps": edge.speed_limit_mps,
        "internal": edge.internal,
    }


def _parse_json_strings(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ComparisonRelationError("expected a JSON list")
    return [str(item) for item in parsed]


def _optional_float(value: str | None) -> float | None:
    return None if value is None else float(value)


def _median(values: list[float | None]) -> float | None:
    present = sorted(float(value) for value in values if value is not None)
    if not present:
        return None
    middle = len(present) // 2
    if len(present) % 2:
        return present[middle]
    return (present[middle - 1] + present[middle]) / 2


def _sum_required(values: list[float | None]) -> float:
    if not values or any(value is None for value in values):
        raise ComparisonRelationError("synthetic relation member length is unavailable")
    return sum(float(value) for value in values if value is not None)


def _render_report(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Comparison relations v2: synthetic boundary repair",
            "",
            "This artifact preserves comparison-relations-v1 and applies one class-level,",
            "traffic-blind ownership rule for unowned netconvert synthetic motorway",
            "continuations at APP relation boundaries.",
            "",
            f"- Parent relations: {summary['parent_relation_count']:,}",
            f"- Changed relations: {summary['changed_relation_count']:,}",
            f"- Added synthetic members: {summary['synthetic_boundary_member_count']:,}",
            "- Builder transition relaxation: none",
            "- SUMO junction-clustering repair: none",
            "",
        ]
    )


def _promote_v2(pending: Path, output: Path) -> None:
    manifest_path = pending / RELATION_MANIFEST
    if output.exists() or not manifest_path.is_file():
        raise ComparisonRelationError("partial or conflicting v2 artifact cannot promote")
    ComparisonRelationManifestV2.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    os.replace(pending, output)
