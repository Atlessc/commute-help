"""Evidence-ranked matching from PORTAL detector stations to directed OSM edges."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable

import geopandas as gpd
import pandas as pd
import pyarrow.dataset as pyarrow_dataset
from pyproj import Transformer
from shapely.geometry import LineString, Point


MATCHER_VERSION = "portal-station-edge-matcher-v1"
MATCH_SCHEMA_VERSION = 1
PROJECTED_CRS = "EPSG:32610"
PORTAL_CRS = "EPSG:3857"
MAX_MATCH_DISTANCE_M = 250.0
MAJOR_ROAD_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
}
LINK_CLASSES = {"motorway_link", "trunk_link", "primary_link", "secondary_link"}
CARDINAL_BEARINGS = {"NORTH": 0.0, "EAST": 90.0, "SOUTH": 180.0, "WEST": 270.0}


class PortalStationMatchError(RuntimeError):
    """The station-matching inputs cannot produce a trustworthy review artifact."""


@dataclass(frozen=True)
class PortalStation:
    station_id: str
    portal_highway_id: int
    highway_name: str
    direction: str
    milepost: float | None
    location_text: str
    longitude: float
    latitude: float
    expected_road_form: str
    target_route_tokens: tuple[str, ...]


@dataclass(frozen=True)
class EdgeCandidate:
    position: int
    edge_id: str
    u: str
    v: str
    key: str
    road_name: str
    road_class: str
    ref: str
    route_tokens: tuple[str, ...]
    bearing_degrees: float
    bearing_difference_degrees: float
    distance_m: float
    route_evidence: str
    road_form_evidence: str
    score: float


def observed_station_ids(observations_directory: Path) -> set[str]:
    """Read the distinct station grain without materializing the observation corpus."""

    if not observations_directory.exists():
        raise PortalStationMatchError(
            f"Processed observations were not found at {observations_directory}."
        )
    dataset = pyarrow_dataset.dataset(
        observations_directory,
        format="parquet",
        partitioning="hive",
    )
    if "station_or_segment_id" not in dataset.schema.names:
        raise PortalStationMatchError(
            "Processed observations do not contain station_or_segment_id."
        )
    station_ids: set[str] = set()
    for batch in dataset.to_batches(
        columns=["station_or_segment_id"], batch_size=262_144
    ):
        station_ids.update(
            str(value).strip()
            for value in batch.column(0).to_pylist()
            if value is not None and str(value).strip()
        )
    return station_ids


def load_portal_stations(
    stations_path: Path,
    highways_path: Path,
    station_ids: set[str],
) -> list[PortalStation]:
    """Load only stations proven to occur in the finalized observation corpus."""

    station_document = _read_json(stations_path)
    highway_document = _read_json(highways_path)
    if not isinstance(station_document, dict) or not isinstance(
        station_document.get("features"), list
    ):
        raise PortalStationMatchError("PORTAL station metadata is not valid GeoJSON.")
    if not isinstance(highway_document, list):
        raise PortalStationMatchError("PORTAL highway metadata must be a JSON array.")
    highways = {
        int(item["highwayid"]): item
        for item in highway_document
        if isinstance(item, dict) and "highwayid" in item
    }
    to_wgs84 = Transformer.from_crs(PORTAL_CRS, "EPSG:4326", always_xy=True)
    stations: list[PortalStation] = []
    seen: set[str] = set()
    for feature in station_document["features"]:
        properties = feature.get("properties") or {}
        raw_station_id = str(properties.get("stationid", "")).strip()
        station_id = f"portal-station-{raw_station_id}"
        if station_id not in station_ids:
            continue
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coordinates, list):
            raise PortalStationMatchError(f"Station {station_id} has no valid point geometry.")
        highway_id = int(properties["highwayid"])
        highway = highways.get(highway_id)
        if highway is None:
            raise PortalStationMatchError(
                f"Station {station_id} references unknown highway {highway_id}."
            )
        longitude, latitude = to_wgs84.transform(
            float(coordinates[0]), float(coordinates[1])
        )
        location_text = str(properties.get("locationtext") or "").strip()
        highway_name = str(highway.get("highwayname") or "").strip()
        stations.append(
            PortalStation(
                station_id=station_id,
                portal_highway_id=highway_id,
                highway_name=highway_name,
                direction=str(highway.get("direction") or "").strip().upper(),
                milepost=_optional_float(properties.get("milepost")),
                location_text=location_text,
                longitude=longitude,
                latitude=latitude,
                expected_road_form=_expected_road_form(location_text),
                target_route_tokens=tuple(
                    sorted(_target_route_tokens(highway_name, location_text))
                ),
            )
        )
        seen.add(station_id)
    missing = sorted(station_ids - seen)
    if missing:
        preview = ", ".join(missing[:5])
        raise PortalStationMatchError(
            f"{len(missing)} observed stations are missing from metadata: {preview}."
        )
    return sorted(stations, key=lambda item: (item.portal_highway_id, item.station_id))


def prepare_edges(edges_path: Path) -> gpd.GeoDataFrame:
    """Load stable graph edge attributes and build the projected spatial index."""

    if not edges_path.exists():
        raise PortalStationMatchError(f"Graph edges were not found at {edges_path}.")
    edges = gpd.read_parquet(edges_path)
    required = {
        "u",
        "v",
        "key",
        "edge_id",
        "road_name",
        "road_class",
        "ref",
        "geometry",
    }
    missing = sorted(required - set(edges.columns))
    if missing:
        raise PortalStationMatchError(
            "Graph edges are missing matcher fields: " + ", ".join(missing) + "."
        )
    if edges.crs is None:
        raise PortalStationMatchError("Graph edges have no coordinate reference system.")
    road_classes = edges["road_class"].map(_primary_road_class)
    edges = edges.loc[road_classes.isin(MAJOR_ROAD_CLASSES)].copy()
    edges["road_class"] = road_classes.loc[edges.index]
    edges = edges.to_crs(PROJECTED_CRS).reset_index(drop=True)
    edges["_bearing"] = edges.geometry.map(_line_bearing)
    edges["_route_tokens"] = edges["ref"].map(_edge_route_tokens)
    _ = edges.sindex
    return edges


def match_stations(
    stations: Iterable[PortalStation],
    edges: gpd.GeoDataFrame,
    *,
    max_distance_m: float = MAX_MATCH_DISTANCE_M,
) -> pd.DataFrame:
    """Score nearby directed candidates and retain uncertainty for human review."""

    to_projected = Transformer.from_crs("EPSG:4326", PROJECTED_CRS, always_xy=True)
    records: list[dict[str, Any]] = []
    minimum_x, minimum_y, maximum_x, maximum_y = edges.total_bounds
    for station in stations:
        x, y = to_projected.transform(station.longitude, station.latitude)
        point = Point(x, y)
        positions = edges.sindex.query(point.buffer(max_distance_m), predicate="intersects")
        candidates = [
            _score_candidate(station, edges.iloc[int(position)], int(position), point)
            for position in positions
        ]
        candidates = sorted(candidates, key=lambda item: (-item.score, item.edge_id))
        outside_graph_extent = not (
            minimum_x <= point.x <= maximum_x and minimum_y <= point.y <= maximum_y
        )
        records.append(
            _match_record(
                station,
                candidates,
                max_distance_m,
                outside_graph_extent=outside_graph_extent,
            )
        )
    return pd.DataFrame.from_records(records).sort_values(
        ["portal_highway_id", "station_id"], ignore_index=True
    )


def summarize_matches(
    matches: pd.DataFrame,
    *,
    graph_version: str,
    source_campaign: str,
) -> dict[str, Any]:
    """Produce machine-readable coverage, confidence, and review findings."""

    total = len(matches)
    status_counts = Counter(matches["status"].astype(str))
    confidence_counts = Counter(matches["confidence"].astype(str))
    accepted = int(status_counts.get("accepted", 0))
    review = int(status_counts.get("review", 0))
    unmatched = int(status_counts.get("unmatched", 0))
    grouped: list[dict[str, Any]] = []
    for (highway_id, highway_name, direction), frame in matches.groupby(
        ["portal_highway_id", "highway_name", "direction"], sort=True
    ):
        counts = Counter(frame["status"].astype(str))
        grouped.append(
            {
                "portal_highway_id": int(highway_id),
                "highway_name": str(highway_name),
                "direction": str(direction),
                "stations": len(frame),
                "accepted": int(counts.get("accepted", 0)),
                "review": int(counts.get("review", 0)),
                "unmatched": int(counts.get("unmatched", 0)),
            }
        )
    matched_distances = pd.to_numeric(matches["distance_m"], errors="coerce").dropna()
    matched_bearings = pd.to_numeric(
        matches["bearing_difference_degrees"], errors="coerce"
    ).dropna()
    findings: list[dict[str, str]] = []
    outside_region = int(
        (
            (matches["status"] == "unmatched")
            & (matches["review_reasons"] == "outside_graph_region")
        ).sum()
    )
    in_region_unmatched = unmatched - outside_region
    if in_region_unmatched:
        findings.append(
            {
                "severity": "high",
                "finding": f"{in_region_unmatched} in-region stations have no safe candidate and cannot calibrate an edge.",
            }
        )
    if outside_region:
        findings.append(
            {
                "severity": "info",
                "finding": f"{outside_region} observed stations are outside this graph's modeled region and are intentionally excluded.",
            }
        )
    if review:
        findings.append(
            {
                "severity": "medium",
                "finding": f"{review} stations need review before their observations are used.",
            }
        )
    if not unmatched and not review:
        findings.append(
            {
                "severity": "info",
                "finding": "Every observed station passed the automatic evidence gate.",
            }
        )
    return {
        "schema_version": MATCH_SCHEMA_VERSION,
        "matcher_version": MATCHER_VERSION,
        "graph_version": graph_version,
        "source_campaign": source_campaign,
        "grain": "one row per observed PORTAL station",
        "station_count": total,
        "accepted_count": accepted,
        "review_count": review,
        "unmatched_count": unmatched,
        "outside_graph_region_count": outside_region,
        "in_region_unmatched_count": in_region_unmatched,
        "accepted_percent": _percent(accepted, total),
        "status_counts": dict(sorted(status_counts.items())),
        "confidence_counts": dict(sorted(confidence_counts.items())),
        "distance_m": _distribution(matched_distances),
        "bearing_difference_degrees": _distribution(matched_bearings),
        "by_highway": grouped,
        "review_reason_counts": dict(
            sorted(
                Counter(
                    reason
                    for value in matches.loc[matches["status"] != "accepted", "review_reasons"]
                    for reason in str(value).split("|")
                    if reason
                ).items()
            )
        ),
        "findings": findings,
    }


def report_markdown(report: dict[str, Any], matches: pd.DataFrame) -> str:
    """Render the review gate as a compact, inspectable Markdown report."""

    lines = [
        "# PORTAL station-to-edge match report",
        "",
        f"- Matcher: `{report['matcher_version']}`",
        f"- Graph: `{report['graph_version']}`",
        f"- Source campaign: `{report['source_campaign']}`",
        f"- Intended grain: {report['grain']}",
        f"- Stations: **{report['station_count']:,}**",
        f"- Automatically accepted: **{report['accepted_count']:,} ({report['accepted_percent']:.2f}%)**",
        f"- Needs review: **{report['review_count']:,}**",
        f"- Unmatched: **{report['unmatched_count']:,}**",
        "",
        "This is a report-only calibration gate. It does not change SQLite or create a selectable traffic profile.",
        "Only `accepted` rows are eligible for the next compilation step. Review and unmatched rows are excluded.",
        "",
        "## Findings",
        "",
    ]
    for finding in report["findings"]:
        lines.append(f"- **{finding['severity'].upper()}** — {finding['finding']}")
    lines.extend(
        [
            "",
            "## Coverage by PORTAL highway",
            "",
            "| ID | Highway | Direction | Stations | Accepted | Review | Unmatched |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in report["by_highway"]:
        lines.append(
            f"| {item['portal_highway_id']} | {item['highway_name']} | {item['direction']} | "
            f"{item['stations']} | {item['accepted']} | {item['review']} | {item['unmatched']} |"
        )
    review_rows = matches.loc[matches["status"] != "accepted"].copy()
    lines.extend(["", "## Rows requiring attention", ""])
    if review_rows.empty:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| Station | PORTAL road | Direction | Status | Candidate | Distance m | Bearing delta | Reasons |",
                "|---|---|---|---|---|---:|---:|---|",
            ]
        )
        for row in review_rows.itertuples(index=False):
            lines.append(
                f"| {row.station_id} | {row.highway_name} | {row.direction} | {row.status} | "
                f"{row.edge_id or ''} | {_display_number(row.distance_m)} | "
                f"{_display_number(row.bearing_difference_degrees)} | {row.review_reasons} |"
            )
    lines.extend(
        [
            "",
            "## Gate logic",
            "",
            "An automatic match must be close enough, align with the directed PORTAL bearing, use a plausible road class, avoid a contradictory route reference, and beat any materially different candidate. Consecutive edge pieces on the same carriageway are treated as equivalent candidates rather than false ambiguity.",
            "",
        ]
    )
    return "\n".join(lines)


def _score_candidate(
    station: PortalStation,
    edge: pd.Series,
    position: int,
    point: Point,
) -> EdgeCandidate:
    distance = float(edge.geometry.distance(point))
    bearing = float(edge["_bearing"])
    target_bearing = CARDINAL_BEARINGS.get(station.direction)
    bearing_difference = (
        _bearing_difference(bearing, target_bearing)
        if target_bearing is not None
        else 180.0
    )
    route_tokens = tuple(edge["_route_tokens"])
    if station.target_route_tokens and route_tokens:
        route_evidence = (
            "match"
            if set(station.target_route_tokens) & set(route_tokens)
            else "mismatch"
        )
    else:
        route_evidence = "unknown"
    road_class = str(edge["road_class"])
    is_link = road_class in LINK_CLASSES
    if station.expected_road_form == "link":
        road_form_evidence = "match" if is_link else "mismatch"
    elif station.expected_road_form == "mainline":
        road_form_evidence = "mismatch" if is_link else "match"
    else:
        road_form_evidence = "unknown"

    distance_score = max(0.0, 42.0 * (1.0 - distance / MAX_MATCH_DISTANCE_M))
    bearing_score = 34.0 * (1.0 - min(bearing_difference, 180.0) / 90.0)
    route_score = {"match": 32.0, "unknown": 0.0, "mismatch": -36.0}[
        route_evidence
    ]
    form_score = {"match": 10.0, "unknown": 0.0, "mismatch": -10.0}[
        road_form_evidence
    ]
    class_score = {
        "motorway": 8.0,
        "trunk": 7.0,
        "motorway_link": 6.0,
        "trunk_link": 5.0,
        "primary": 2.0,
        "primary_link": 1.0,
        "secondary": -4.0,
        "secondary_link": -4.0,
    }.get(road_class, -10.0)
    return EdgeCandidate(
        position=position,
        edge_id=str(edge["edge_id"]),
        u=str(edge["u"]),
        v=str(edge["v"]),
        key=str(edge["key"]),
        road_name=_clean_text(edge.get("road_name")),
        road_class=road_class,
        ref=_clean_text(edge.get("ref")),
        route_tokens=route_tokens,
        bearing_degrees=round(bearing, 3),
        bearing_difference_degrees=round(bearing_difference, 3),
        distance_m=round(distance, 3),
        route_evidence=route_evidence,
        road_form_evidence=road_form_evidence,
        score=round(distance_score + bearing_score + route_score + form_score + class_score, 3),
    )


def _match_record(
    station: PortalStation,
    candidates: list[EdgeCandidate],
    max_distance_m: float,
    *,
    outside_graph_extent: bool = False,
) -> dict[str, Any]:
    base = asdict(station)
    base["target_route_tokens"] = "|".join(station.target_route_tokens)
    base.update(
        {
            "matcher_version": MATCHER_VERSION,
            "status": "unmatched",
            "confidence": "none",
            "review_reasons": (
                "outside_graph_region"
                if outside_graph_extent
                else "no_candidate_within_radius"
            ),
            "candidate_count": len(candidates),
            "equivalent_candidate_count": 0,
            "edge_id": "",
            "u": "",
            "v": "",
            "key": "",
            "road_name": "",
            "road_class": "",
            "edge_ref": "",
            "edge_route_tokens": "",
            "distance_m": math.nan,
            "bearing_degrees": math.nan,
            "bearing_difference_degrees": math.nan,
            "route_evidence": "none",
            "road_form_evidence": "none",
            "score": math.nan,
            "competitor_score": math.nan,
            "score_margin": math.nan,
        }
    )
    if not candidates:
        return base
    best = candidates[0]
    equivalent = [
        candidate
        for candidate in candidates[1:]
        if _equivalent_carriageway_candidate(best, candidate)
    ]
    competitor = next(
        (
            candidate
            for candidate in candidates[1:]
            if not _equivalent_carriageway_candidate(best, candidate)
        ),
        None,
    )
    margin = best.score - competitor.score if competitor is not None else math.inf
    reasons: list[str] = []
    if best.distance_m > min(max_distance_m, 100.0):
        reasons.append("distance_over_100m")
    if best.bearing_difference_degrees > 55.0:
        reasons.append("direction_mismatch")
    if best.route_evidence == "mismatch":
        reasons.append("route_reference_mismatch")
    if best.road_class not in MAJOR_ROAD_CLASSES:
        reasons.append("implausible_road_class")
    if best.score < 45.0:
        reasons.append("weak_evidence_score")
    if margin < 8.0:
        reasons.append("ambiguous_competing_edge")

    status = "accepted" if not reasons else "review"
    confidence = "high" if status == "accepted" else "medium"
    base.update(
        {
            "status": status,
            "confidence": confidence,
            "review_reasons": "|".join(reasons),
            "candidate_count": len(candidates),
            "equivalent_candidate_count": len(equivalent),
            "edge_id": best.edge_id,
            "u": best.u,
            "v": best.v,
            "key": best.key,
            "road_name": best.road_name,
            "road_class": best.road_class,
            "edge_ref": best.ref,
            "edge_route_tokens": "|".join(best.route_tokens),
            "distance_m": best.distance_m,
            "bearing_degrees": best.bearing_degrees,
            "bearing_difference_degrees": best.bearing_difference_degrees,
            "route_evidence": best.route_evidence,
            "road_form_evidence": best.road_form_evidence,
            "score": best.score,
            "competitor_score": competitor.score if competitor is not None else math.nan,
            "score_margin": round(margin, 3) if math.isfinite(margin) else math.nan,
        }
    )
    return base


def _equivalent_carriageway_candidate(
    first: EdgeCandidate, second: EdgeCandidate
) -> bool:
    same_link_family = (first.road_class in LINK_CLASSES) == (
        second.road_class in LINK_CLASSES
    )
    compatible_routes = (
        not first.route_tokens
        or not second.route_tokens
        or bool(set(first.route_tokens) & set(second.route_tokens))
    )
    return (
        same_link_family
        and compatible_routes
        and _bearing_difference(first.bearing_degrees, second.bearing_degrees) <= 25.0
    )


def _target_route_tokens(highway_name: str, location_text: str) -> set[str]:
    # DS is an agency grouping, not a road. Its location text carries the useful route.
    source = location_text if highway_name.strip().upper() == "DS" else highway_name
    return _route_tokens(source)


def _edge_route_tokens(value: Any) -> tuple[str, ...]:
    return tuple(sorted(_route_tokens(_clean_text(value))))


def _route_tokens(value: str) -> set[str]:
    text = value.upper().replace("–", "-")
    patterns = (
        (r"\bI\s*[- ]?\s*(\d{1,3})\b", "I"),
        (r"\bUS\s*[- ]?\s*(\d{1,3})\b", "US"),
        (r"\b(?:OR|OREGON)\s*[- ]?\s*(\d{1,3})\b", "OR"),
        (r"\b(?:WA|SR)\s*[- ]?\s*(\d{1,3})\b", "WA"),
    )
    tokens: set[str] = set()
    for pattern, prefix in patterns:
        tokens.update(f"{prefix}{match}" for match in re.findall(pattern, text))
    return tokens


def _expected_road_form(location_text: str) -> str:
    text = location_text.lower()
    if any(word in text for word in ("ramp", "frontage", " hov")):
        return "link"
    if re.search(r"\bto\s+(?:nb|sb|eb|wb)\b", text):
        return "link"
    if " @ " in text:
        return "mainline"
    return "unknown"


def _primary_road_class(value: Any) -> str:
    text = _clean_text(value)
    if text.startswith("["):
        try:
            values = json.loads(text)
            if isinstance(values, list) and values:
                return str(values[0])
        except json.JSONDecodeError:
            pass
    return text


def _line_bearing(geometry: Any) -> float:
    if not isinstance(geometry, LineString) or len(geometry.coords) < 2:
        return 0.0
    start_x, start_y = geometry.coords[0][:2]
    end_x, end_y = geometry.coords[-1][:2]
    return (math.degrees(math.atan2(end_x - start_x, end_y - start_y)) + 360.0) % 360.0


def _bearing_difference(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _distribution(values: pd.Series) -> dict[str, float | None]:
    if values.empty:
        return {"median": None, "p85": None, "p95": None, "maximum": None}
    return {
        "median": round(float(values.median()), 3),
        "p85": round(float(values.quantile(0.85)), 3),
        "p95": round(float(values.quantile(0.95)), 3),
        "maximum": round(float(values.max()), 3),
    }


def _percent(numerator: int, denominator: int) -> float:
    return round(numerator / max(denominator, 1) * 100.0, 2)


def _display_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.1f}" if math.isfinite(number) else ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PortalStationMatchError(f"Could not read {path}.") from error
