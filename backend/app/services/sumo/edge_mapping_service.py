"""Versioned, evidence-ranked mapping from app graph edges to directed SUMO edges."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
import sumolib
from shapely.geometry import LineString
from shapely.ops import unary_union


Log = Callable[[str], None]


@dataclass(frozen=True)
class SumoEdgeEvidence:
    edge_id: str
    from_node: str
    to_node: str
    lane_ids: tuple[str, ...]
    osm_way_ids: tuple[str, ...]
    road_name: str
    road_class: str
    geometry: LineString


def build_edge_map(
    *,
    app_edges_path: Path,
    app_graph_manifest_path: Path,
    sumo_network_path: Path,
    sumo_network_manifest_path: Path,
    output_dir: Path,
    log: Log = print,
) -> dict[str, Any]:
    """Build mapping artifacts, failing when network source lineage differs."""

    started = monotonic()
    app_manifest = json.loads(app_graph_manifest_path.read_text(encoding="utf-8"))
    sumo_manifest = json.loads(sumo_network_manifest_path.read_text(encoding="utf-8"))
    app_source_sha = app_manifest.get("osm_source_sha256")
    sumo_source_sha = sumo_manifest.get("osm_source_sha256")
    if not app_source_sha or app_source_sha != sumo_source_sha:
        raise ValueError("App and SUMO networks do not share the same frozen OSM source")
    if _sha256(app_edges_path) != app_manifest["artifacts"]["edges_parquet"]["sha256"]:
        raise ValueError("App edges checksum does not match its graph manifest")
    if _sha256(sumo_network_path) != sumo_manifest["artifact"]["sha256"]:
        raise ValueError("SUMO network checksum does not match its manifest")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "edge-map.parquet"
    report_path = output_dir / "edge-map-report.json"
    if output_path.exists() or report_path.exists():
        raise ValueError("Edge-map output already exists; use a new network version")

    log("LOAD app edge geometries")
    app_edges = gpd.read_parquet(app_edges_path)
    if app_edges.crs is None:
        app_edges = app_edges.set_crs("EPSG:4326")
    app_projected = app_edges.to_crs("EPSG:32610")

    log("LOAD SUMO network")
    network = sumolib.net.readNet(str(sumo_network_path), withInternal=False)
    sumo_evidence = _sumo_edges(network)
    sumo_geographic = gpd.GeoSeries(
        [edge.geometry for edge in sumo_evidence],
        crs="EPSG:4326",
    )
    sumo_projected = sumo_geographic.to_crs("EPSG:32610")
    by_osm: dict[str, list[int]] = defaultdict(list)
    for index, edge in enumerate(sumo_evidence):
        for osm_id in edge.osm_way_ids:
            by_osm[osm_id].append(index)

    records: list[dict[str, Any]] = []
    status_counts = {"accepted": 0, "review": 0, "unmatched": 0}
    accepted_relation_count = 0
    for position, (_, app_edge) in enumerate(app_projected.iterrows(), start=1):
        app_osm_ids = _app_osm_ids(app_edge.get("osm_way_ids"), app_edge.get("osmid"))
        candidate_indices = sorted(
            {candidate for osm_id in app_osm_ids for candidate in by_osm.get(osm_id, [])}
        )
        app_geometry = app_edge.geometry
        app_u = str(app_edge["u"])
        app_v = str(app_edge["v"])
        buffer = app_geometry.buffer(12.0)
        selected: list[tuple[int, float, float, float, bool]] = []
        for candidate_index in candidate_indices:
            candidate = sumo_evidence[candidate_index]
            candidate_geometry = sumo_projected.iloc[candidate_index]
            distance = float(app_geometry.distance(candidate_geometry))
            direction_error = _direction_error(app_geometry, candidate_geometry)
            overlap = (
                float(candidate_geometry.intersection(buffer).length / candidate_geometry.length)
                if candidate_geometry.length > 0
                else 0.0
            )
            exact_endpoints = candidate.from_node == app_u and candidate.to_node == app_v
            if direction_error <= 45 and (overlap >= 0.55 or exact_endpoints):
                selected.append(
                    (candidate_index, distance, direction_error, overlap, exact_endpoints)
                )

        exact_endpoint_candidate = any(item[4] for item in selected)
        if selected:
            selected_geometries = [sumo_projected.iloc[item[0]] for item in selected]
            covered = unary_union(selected_geometries).buffer(12.0)
            coverage = (
                float(app_geometry.intersection(covered).length / app_geometry.length)
                if app_geometry.length > 0
                else 0.0
            )
            max_direction_error = max(item[2] for item in selected)
            status = _mapping_status(
                coverage=coverage,
                max_direction_error=max_direction_error,
                exact_endpoint_candidate=exact_endpoint_candidate,
            )
            reason = None if status == "accepted" else "insufficient_geometry_coverage"
        else:
            coverage = 0.0
            status = "unmatched"
            reason = "no_directional_osm_geometry_candidate"

        status_counts[status] += 1
        base = {
            "graph_version": app_manifest["graph_version"],
            "sumo_network_version": sumo_manifest["network_version"],
            "osm_source_sha256": app_source_sha,
            "app_edge_id": str(app_edge["edge_id"]),
            "app_u": app_u,
            "app_v": app_v,
            "app_key": int(app_edge["key"]),
            "app_osm_way_ids": json.dumps(app_osm_ids, separators=(",", ":")),
            "direction_signature": f"{app_u}->{app_v}",
            "road_name": str(app_edge.get("road_name") or "Unnamed road"),
            "road_class": str(app_edge.get("road_class") or "unclassified"),
            "app_geometry_wkb": bytes(app_geometry.wkb),
            "coverage_ratio": coverage,
            "exact_endpoint_candidate": exact_endpoint_candidate,
            "status": status,
            "review_reason": reason,
        }
        if not selected:
            records.append(
                {
                    **base,
                    "sumo_edge_id": None,
                    "sumo_lane_ids": "[]",
                    "sumo_osm_way_ids": "[]",
                    "sumo_from": None,
                    "sumo_to": None,
                    "sumo_geometry_wkb": None,
                    "match_method": "none",
                    "match_score": 0.0,
                    "match_distance_m": None,
                    "direction_error_degrees": None,
                }
            )
        else:
            for candidate_index, distance, direction_error, overlap, exact in selected:
                candidate = sumo_evidence[candidate_index]
                score = max(0.0, min(1.0, coverage * (1 - direction_error / 180)))
                records.append(
                    {
                        **base,
                        "sumo_edge_id": candidate.edge_id,
                        "sumo_lane_ids": json.dumps(candidate.lane_ids, separators=(",", ":")),
                        "sumo_osm_way_ids": json.dumps(
                            candidate.osm_way_ids,
                            separators=(",", ":"),
                        ),
                        "sumo_from": candidate.from_node,
                        "sumo_to": candidate.to_node,
                        "sumo_geometry_wkb": bytes(sumo_projected.iloc[candidate_index].wkb),
                        "match_method": "osm_endpoint" if exact else "osm_geometry_chain",
                        "match_score": score,
                        "match_distance_m": distance,
                        "direction_error_degrees": direction_error,
                        "candidate_overlap_ratio": overlap,
                    }
                )
                if status == "accepted":
                    accepted_relation_count += 1

        if position % 25_000 == 0:
            log(
                f"MAP {position:,}/{len(app_projected):,} accepted={status_counts['accepted']:,} "
                f"review={status_counts['review']:,} unmatched={status_counts['unmatched']:,} "
                f"elapsed={monotonic() - started:0.1f}s"
            )

    mapping = pd.DataFrame.from_records(records)
    mapping.to_parquet(output_path, index=False)
    review_columns = [
        "app_edge_id",
        "app_u",
        "app_v",
        "road_name",
        "road_class",
        "app_osm_way_ids",
        "coverage_ratio",
        "status",
        "review_reason",
    ]
    mapping.loc[mapping["status"] != "accepted", review_columns].drop_duplicates(
        subset=["app_edge_id"]
    ).to_csv(output_dir / "edge-map-review.csv", index=False)
    total = len(app_projected)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "graph_version": app_manifest["graph_version"],
        "sumo_network_version": sumo_manifest["network_version"],
        "osm_source_sha256": app_source_sha,
        "app_edge_count": total,
        "sumo_edge_count": len(sumo_evidence),
        "mapping_relation_count": len(mapping),
        "accepted_relation_count": accepted_relation_count,
        "status_counts": status_counts,
        "accepted_percent": round(100 * status_counts["accepted"] / total, 3),
        "thresholds": {
            "candidate_buffer_m": 12.0,
            "candidate_overlap_min": 0.55,
            "accepted_coverage_min": 0.88,
            "accepted_exact_endpoint_coverage_min": 0.70,
            "direction_error_max_degrees": 45.0,
        },
        "artifacts": {
            "edge_map": {
                "filename": output_path.name,
                "sha256": _sha256(output_path),
                "size_bytes": output_path.stat().st_size,
            },
            "review_csv": {
                "filename": "edge-map-review.csv",
                "sha256": _sha256(output_dir / "edge-map-review.csv"),
                "size_bytes": (output_dir / "edge-map-review.csv").stat().st_size,
            },
        },
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output_dir / "edge-map-report.md").write_text(
        "\n".join(
            [
                "# App-to-SUMO edge mapping",
                "",
                f"- App graph: `{report['graph_version']}`",
                f"- SUMO network: `{report['sumo_network_version']}`",
                f"- App edges: {total:,}",
                f"- Accepted: {status_counts['accepted']:,} ({report['accepted_percent']:.3f}%)",
                f"- Review: {status_counts['review']:,}",
                f"- Unmatched: {status_counts['unmatched']:,}",
                "",
                "Only `accepted` mappings may translate user closures into SUMO.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(
        f"COMPLETE accepted={status_counts['accepted']:,}/{total:,} "
        f"review={status_counts['review']:,} unmatched={status_counts['unmatched']:,}"
    )
    return report


def _sumo_edges(network: Any) -> list[SumoEdgeEvidence]:
    evidence: list[SumoEdgeEvidence] = []
    for edge in network.getEdges(withInternal=False):
        lane_ids = tuple(lane.getID() for lane in edge.getLanes())
        osm_ids = sorted(
            {
                osm_id
                for lane in edge.getLanes()
                for osm_id in lane.getParams().get("origId", "").split()
                if osm_id
            }
        )
        if not osm_ids:
            raw_id = edge.getID().split("#", 1)[0].lstrip("-")
            if raw_id.isdigit():
                osm_ids = [raw_id]
        geographic_shape = [network.convertXY2LonLat(x, y) for x, y in edge.getShape()]
        if len(geographic_shape) < 2:
            continue
        evidence.append(
            SumoEdgeEvidence(
                edge_id=edge.getID(),
                from_node=edge.getFromNode().getID(),
                to_node=edge.getToNode().getID(),
                lane_ids=lane_ids,
                osm_way_ids=tuple(osm_ids),
                road_name=edge.getName() or "Unnamed road",
                road_class=edge.getType().removeprefix("highway.") or "unclassified",
                geometry=LineString(geographic_shape),
            )
        )
    return evidence


def _app_osm_ids(primary: Any, fallback: Any) -> tuple[str, ...]:
    values: Any = primary
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError:
            values = [values]
    if values is None:
        values = fallback
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return tuple(sorted({str(value) for value in values if value is not None}))


def _direction_error(first: LineString, second: LineString) -> float:
    first_start = first.coords[0]
    first_end = first.coords[-1]
    second_start = second.coords[0]
    second_end = second.coords[-1]
    first_angle = math.degrees(
        math.atan2(first_end[1] - first_start[1], first_end[0] - first_start[0])
    )
    second_angle = math.degrees(
        math.atan2(second_end[1] - second_start[1], second_end[0] - second_start[0])
    )
    difference = abs((first_angle - second_angle + 180) % 360 - 180)
    return float(difference)


def _mapping_status(
    *,
    coverage: float,
    max_direction_error: float,
    exact_endpoint_candidate: bool,
) -> str:
    """Apply the conservative automatic-acceptance rule.

    SUMO trims external lane geometry at junction boundaries.  A directed edge
    can therefore have less than 88 percent visible shape coverage even when
    SUMO preserved the exact OSM from/to node identity.  The lower threshold is
    allowed only with that stronger endpoint evidence; geometry proximity alone
    never receives the exception.
    """

    if max_direction_error > 45.0:
        return "review"
    if coverage >= 0.88:
        return "accepted"
    if exact_endpoint_candidate and coverage >= 0.70:
        return "accepted"
    return "review"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
