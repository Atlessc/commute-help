"""Build an evidence-ranked prior table for every directed graph edge.

The output is an analytical artifact. It does not mutate the routing graph or
promote inferred values to measured observations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString


SCHEMA_VERSION = 1
BUILDER_VERSION = "edge-prior-builder-v1"
PROJECTED_CRS = "EPSG:32610"
MPH_TO_KPH = 1.609344
AUTHORITY_MATCH_DISTANCE_M = 18.0
SIGNAL_NODE_DISTANCE_M = 35.0
STOP_NODE_DISTANCE_M = 25.0

ROAD_CLASS_SPEED_KPH = {
    "motorway": 100.0,
    "motorway_link": 70.0,
    "trunk": 80.0,
    "trunk_link": 60.0,
    "primary": 60.0,
    "primary_link": 50.0,
    "secondary": 50.0,
    "secondary_link": 45.0,
    "tertiary": 45.0,
    "tertiary_link": 40.0,
    "unclassified": 35.0,
    "residential": 30.0,
    "living_street": 15.0,
    "service": 20.0,
    "track": 15.0,
}

ROAD_CLASS_LANES = {
    "motorway": 3,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 1,
    "primary": 2,
    "primary_link": 1,
    "secondary": 2,
    "secondary_link": 1,
    "tertiary": 2,
    "tertiary_link": 1,
    "unclassified": 1,
    "residential": 1,
    "living_street": 1,
    "service": 1,
    "track": 1,
}

CAPACITY_PER_LANE_VPH = {
    "motorway": 2000.0,
    "motorway_link": 1600.0,
    "trunk": 1800.0,
    "trunk_link": 1500.0,
    "primary": 1600.0,
    "primary_link": 1300.0,
    "secondary": 1400.0,
    "secondary_link": 1200.0,
    "tertiary": 1200.0,
    "tertiary_link": 1000.0,
    "unclassified": 900.0,
    "residential": 700.0,
    "living_street": 400.0,
    "service": 400.0,
    "track": 250.0,
}


class EdgePriorBuildError(RuntimeError):
    """The local inputs cannot produce a trustworthy edge-prior artifact."""


@dataclass(frozen=True)
class AuthoritySource:
    name: str
    directory: str
    speed_field: str
    name_field: str
    id_field: str
    geometry_format: str = "geojson"
    lanes_field: str | None = None
    adt_field: str | None = None
    adt_year_field: str | None = None


AUTHORITY_SPEED_SOURCES = (
    AuthoritySource(
        "authority_pbot",
        "pbot_speed_limits",
        "SpeedLimit",
        "RoadName",
        "OBJECTID",
    ),
    AuthoritySource(
        "authority_odot",
        "odot_posted_speed",
        "SPEED",
        "HWYNAME",
        "OBJECTID",
        geometry_format="esri_json",
    ),
    AuthoritySource(
        "authority_wsdot",
        "wsdot_legal_speed",
        "SpeedLimit",
        "RouteIdentifier",
        "OBJECTID",
    ),
    AuthoritySource(
        "authority_clark",
        "clark_county_road_log",
        "SpeedLimit",
        "RoadName",
        "OBJECTID",
        lanes_field="NumThruLanes",
        adt_field="ADTVolume",
        adt_year_field="ADTYear",
    ),
)


def load_complete_campaign(campaign_directory: Path) -> dict[str, Any]:
    manifest_path = campaign_directory / "campaign-manifest.json"
    if not manifest_path.exists():
        raise EdgePriorBuildError(f"Campaign manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "complete":
        raise EdgePriorBuildError("Authority-controls campaign is not complete.")
    partials = list(campaign_directory.rglob("*.part"))
    if partials:
        raise EdgePriorBuildError(
            f"Authority-controls campaign still contains {len(partials)} partial files."
        )
    return manifest


def build_edge_priors(
    *,
    edges_path: Path,
    nodes_path: Path,
    graph_version: str,
    campaign_directory: Path,
    traffic_profiles_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, dict[str, Any]]:
    """Return all-edge priors, authority review rows, and a quality report."""

    log = progress or (lambda _message: None)
    manifest = load_complete_campaign(campaign_directory)
    edges = _prepare_edges(edges_path)
    nodes = _prepare_nodes(nodes_path)
    log(f"loaded {len(edges):,} directed edges and {len(nodes):,} nodes")

    candidates: list[pd.DataFrame] = []
    source_counts: dict[str, int] = {}
    for source in AUTHORITY_SPEED_SOURCES:
        authority = _load_authority_lines(campaign_directory, source)
        source_counts[source.name] = len(authority)
        log(f"matching {len(authority):,} {source.name} speed segments")
        candidates.append(_match_authority_lines(edges, authority, source.name))
    candidate_frame = pd.concat(candidates, ignore_index=True) if candidates else pd.DataFrame()
    selected, review = _select_authority_matches(candidate_frame)
    log(
        f"accepted authority speed matches for {len(selected):,} edges; "
        f"retained {len(review):,} ambiguous/rejected review rows"
    )

    priors = edges.copy()
    priors = priors.merge(selected, on="edge_id", how="left", validate="one_to_one")
    _resolve_speed_priors(priors)
    _resolve_lane_and_capacity_priors(priors)
    _attach_controls(priors, nodes, campaign_directory, log)
    _attach_traffic_evidence(priors, traffic_profiles_path, log)

    priors["prior_schema_version"] = SCHEMA_VERSION
    priors["prior_builder_version"] = BUILDER_VERSION
    priors["graph_version"] = graph_version
    priors["authority_campaign_id"] = str(manifest.get("campaignId") or campaign_directory.name)
    output_columns = [
        "prior_schema_version",
        "prior_builder_version",
        "graph_version",
        "authority_campaign_id",
        "edge_id",
        "u",
        "v",
        "key",
        "osm_way_ids",
        "geometry_fingerprint",
        "road_name",
        "road_class",
        "ref",
        "length_m",
        "geometry",
        "osm_maxspeed_raw",
        "osm_explicit_speed_kph",
        "authority_speed_kph",
        "resolved_speed_kph",
        "speed_source",
        "speed_confidence",
        "authority_source_id",
        "authority_match_score",
        "authority_distance_m",
        "authority_overlap_fraction",
        "authority_angle_difference_degrees",
        "authority_name_agreement",
        "osm_lanes_raw",
        "osm_lanes_estimated",
        "resolved_lanes",
        "lanes_source",
        "lanes_confidence",
        "authority_total_thru_lanes",
        "authority_adt",
        "authority_adt_year",
        "estimated_capacity_vph",
        "capacity_source",
        "capacity_confidence",
        "free_flow_seconds",
        "downstream_node_id",
        "downstream_control_type",
        "control_source",
        "control_confidence",
        "control_timing_available",
        "official_stop_signs_nearby",
        "stop_directionality",
        "historical_am_available",
        "historical_am_observations",
        "historical_am_speed_kph",
        "historical_am_volume_vph",
        "historical_pm_available",
        "historical_pm_observations",
        "historical_pm_speed_kph",
        "historical_pm_volume_vph",
        "traffic_evidence",
    ]
    priors = gpd.GeoDataFrame(priors[output_columns], geometry="geometry", crs=edges.crs)
    report = _build_report(priors, review, source_counts, manifest)
    return priors, review, report


def _prepare_edges(path: Path) -> gpd.GeoDataFrame:
    if not path.exists():
        raise EdgePriorBuildError(f"Graph edges not found: {path}")
    edges = gpd.read_parquet(path)
    required = {
        "edge_id",
        "u",
        "v",
        "key",
        "geometry",
        "road_name",
        "road_class",
        "ref",
        "maxspeed",
        "lanes",
        "lanes_estimated",
        "length_m",
        "osm_way_ids",
        "geometry_fingerprint",
    }
    missing = sorted(required - set(edges.columns))
    if missing:
        raise EdgePriorBuildError("Graph edges missing fields: " + ", ".join(missing))
    if edges.crs is None:
        raise EdgePriorBuildError("Graph edges have no CRS.")
    edges = edges.copy()
    edges["road_class"] = edges["road_class"].map(_primary_value)
    edges["edge_name_normalized"] = edges["road_name"].map(_normalize_road_name)
    edges["edge_ref_normalized"] = edges["ref"].map(_normalize_ref)
    edges = edges.to_crs(PROJECTED_CRS).reset_index(drop=True)
    edges["edge_bearing"] = edges.geometry.map(_line_bearing)
    edges["edge_geometry_length"] = edges.geometry.length.clip(lower=0.1)
    _ = edges.sindex
    return edges


def _prepare_nodes(path: Path) -> gpd.GeoDataFrame:
    if not path.exists():
        raise EdgePriorBuildError(f"Graph nodes not found: {path}")
    nodes = gpd.read_parquet(path)
    required = {"osmid", "highway", "geometry"}
    missing = sorted(required - set(nodes.columns))
    if missing:
        raise EdgePriorBuildError("Graph nodes missing fields: " + ", ".join(missing))
    if nodes.crs is None:
        raise EdgePriorBuildError("Graph nodes have no CRS.")
    return nodes.to_crs(PROJECTED_CRS).reset_index(drop=True)


def _load_authority_lines(
    campaign_directory: Path, source: AuthoritySource
) -> gpd.GeoDataFrame:
    records: list[dict[str, Any]] = []
    source_directory = campaign_directory / source.directory
    suffix = "*.json" if source.geometry_format == "esri_json" else "*.geojson"
    for path in sorted(source_directory.glob(suffix)):
        if path.name in {"metadata.json", "object-ids.json"}:
            continue
        document = json.loads(path.read_text())
        for feature in document.get("features", []):
            properties = feature.get("properties") or feature.get("attributes") or {}
            speed_mph = _positive_float(properties.get(source.speed_field))
            geometry = _feature_line(feature.get("geometry"), source.geometry_format)
            if speed_mph is None or geometry is None:
                continue
            records.append(
                {
                    "source_feature_id": str(properties.get(source.id_field) or ""),
                    "source_road_name": str(properties.get(source.name_field) or ""),
                    "source_name_normalized": _normalize_road_name(
                        properties.get(source.name_field)
                    ),
                    "source_ref_normalized": _normalize_ref(
                        properties.get(source.name_field)
                    ),
                    "authority_speed_kph": speed_mph * MPH_TO_KPH,
                    "authority_total_thru_lanes": _positive_float(
                        properties.get(source.lanes_field)
                    )
                    if source.lanes_field
                    else None,
                    "authority_adt": _positive_float(properties.get(source.adt_field))
                    if source.adt_field
                    else None,
                    "authority_adt_year": _optional_int(properties.get(source.adt_year_field))
                    if source.adt_year_field
                    else None,
                    "geometry": geometry,
                }
            )
    frame = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    if frame.empty:
        raise EdgePriorBuildError(f"No usable line features found for {source.name}.")
    frame = frame.to_crs(PROJECTED_CRS).reset_index(drop=True)
    frame["source_bearing"] = frame.geometry.map(_line_bearing)
    return frame


def _match_authority_lines(
    edges: gpd.GeoDataFrame,
    authority: gpd.GeoDataFrame,
    source_name: str,
) -> pd.DataFrame:
    buffers = authority[
        [
            "source_feature_id",
            "source_road_name",
            "source_name_normalized",
            "source_ref_normalized",
            "authority_speed_kph",
            "authority_total_thru_lanes",
            "authority_adt",
            "authority_adt_year",
            "source_bearing",
            "geometry",
        ]
    ].copy()
    source_geometries = dict(enumerate(buffers.geometry))
    buffers.geometry = buffers.geometry.buffer(AUTHORITY_MATCH_DISTANCE_M)
    pairs = gpd.sjoin(
        edges[
            [
                "edge_id",
                "edge_name_normalized",
                "edge_ref_normalized",
                "edge_bearing",
                "edge_geometry_length",
                "geometry",
            ]
        ],
        buffers,
        how="inner",
        predicate="intersects",
    ).reset_index(drop=True)
    records: list[dict[str, Any]] = []
    for row in pairs.itertuples(index=False):
        source_geometry = source_geometries[int(row.index_right)]
        distance = float(row.geometry.distance(source_geometry))
        angle = _undirected_angle_difference(
            float(row.edge_bearing), float(row.source_bearing)
        )
        overlap = float(
            row.geometry.intersection(
                source_geometry.buffer(AUTHORITY_MATCH_DISTANCE_M * 0.67)
            ).length
            / max(float(row.edge_geometry_length), 0.1)
        )
        name_agreement = _names_agree(
            row.edge_name_normalized,
            row.edge_ref_normalized,
            row.source_name_normalized,
            row.source_ref_normalized,
        )
        eligible = (
            distance <= AUTHORITY_MATCH_DISTANCE_M
            and angle <= 35.0
            and overlap >= 0.45
            and (name_agreement or (overlap >= 0.82 and angle <= 15.0))
        )
        score = (
            min(overlap, 1.0) * 60.0
            + max(0.0, 1.0 - distance / AUTHORITY_MATCH_DISTANCE_M) * 20.0
            + max(0.0, 1.0 - angle / 35.0) * 10.0
            + (10.0 if name_agreement else 0.0)
        )
        records.append(
            {
                "edge_id": row.edge_id,
                "authority_source": source_name,
                "authority_source_id": str(row.source_feature_id),
                "authority_speed_kph": float(row.authority_speed_kph),
                "authority_total_thru_lanes": row.authority_total_thru_lanes,
                "authority_adt": row.authority_adt,
                "authority_adt_year": row.authority_adt_year,
                "authority_match_score": round(score, 4),
                "authority_distance_m": round(distance, 3),
                "authority_overlap_fraction": round(min(overlap, 1.0), 4),
                "authority_angle_difference_degrees": round(angle, 3),
                "authority_name_agreement": bool(name_agreement),
                "eligible": eligible,
            }
        )
    return pd.DataFrame.from_records(records)


def _select_authority_matches(
    candidates: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected_records: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []
    if candidates.empty:
        return pd.DataFrame(columns=["edge_id"]), candidates
    for edge_id, frame in candidates.groupby("edge_id", sort=False):
        ranked = frame.sort_values(
            ["eligible", "authority_match_score", "authority_source_id"],
            ascending=[False, False, True],
        )
        eligible = ranked.loc[ranked["eligible"]]
        if eligible.empty:
            best = ranked.iloc[0].to_dict()
            best["review_reason"] = "below_acceptance_threshold"
            review_records.append(best)
            continue
        best = eligible.iloc[0]
        ambiguous = False
        if len(eligible) > 1:
            second = eligible.iloc[1]
            ambiguous = (
                float(best["authority_match_score"])
                - float(second["authority_match_score"])
                < 4.0
                and abs(
                    float(best["authority_speed_kph"])
                    - float(second["authority_speed_kph"])
                )
                > 0.5 * MPH_TO_KPH
            )
        if ambiguous:
            for record in eligible.head(2).to_dict("records"):
                record["review_reason"] = "competing_speed_values"
                review_records.append(record)
            continue
        confidence = (
            "high"
            if float(best["authority_overlap_fraction"]) >= 0.8
            and float(best["authority_distance_m"]) <= 8.0
            and float(best["authority_angle_difference_degrees"]) <= 15.0
            else "medium"
        )
        selected_records.append(
            {
                "edge_id": edge_id,
                **{
                    key: best[key]
                    for key in (
                        "authority_source",
                        "authority_source_id",
                        "authority_speed_kph",
                        "authority_total_thru_lanes",
                        "authority_adt",
                        "authority_adt_year",
                        "authority_match_score",
                        "authority_distance_m",
                        "authority_overlap_fraction",
                        "authority_angle_difference_degrees",
                        "authority_name_agreement",
                    )
                },
                "authority_match_confidence": confidence,
            }
        )
    return (
        pd.DataFrame.from_records(selected_records),
        pd.DataFrame.from_records(review_records),
    )


def _resolve_speed_priors(priors: gpd.GeoDataFrame) -> None:
    priors["_osm_speed_kph"] = priors["maxspeed"].map(_parse_osm_speed_kph)
    priors["osm_maxspeed_raw"] = priors["maxspeed"].map(_stable_raw_value)
    priors["osm_explicit_speed_kph"] = priors["_osm_speed_kph"]
    priors["resolved_speed_kph"] = priors["authority_speed_kph"]
    priors["speed_source"] = priors["authority_source"]
    priors["speed_confidence"] = priors["authority_match_confidence"]

    osm_mask = priors["resolved_speed_kph"].isna() & priors["_osm_speed_kph"].notna()
    priors.loc[osm_mask, "resolved_speed_kph"] = priors.loc[osm_mask, "_osm_speed_kph"]
    priors.loc[osm_mask, "speed_source"] = "osm_explicit"
    priors.loc[osm_mask, "speed_confidence"] = "medium"

    neighbor_values = _corridor_neighbor_values(priors, "resolved_speed_kph")
    neighbor_mask = priors["resolved_speed_kph"].isna() & neighbor_values.notna()
    priors.loc[neighbor_mask, "resolved_speed_kph"] = neighbor_values.loc[neighbor_mask]
    priors.loc[neighbor_mask, "speed_source"] = "same_corridor_neighbor"
    priors.loc[neighbor_mask, "speed_confidence"] = "low"

    default_mask = priors["resolved_speed_kph"].isna()
    priors.loc[default_mask, "resolved_speed_kph"] = priors.loc[
        default_mask, "road_class"
    ].map(ROAD_CLASS_SPEED_KPH).fillna(30.0)
    priors.loc[default_mask, "speed_source"] = "road_class_default"
    priors.loc[default_mask, "speed_confidence"] = "low"
    priors["free_flow_seconds"] = priors["length_m"] / (
        priors["resolved_speed_kph"] / 3.6
    )


def _resolve_lane_and_capacity_priors(priors: gpd.GeoDataFrame) -> None:
    explicit_lane_mask = ~priors["lanes_estimated"].fillna(True).astype(bool)
    priors["osm_lanes_raw"] = priors["lanes"].map(_stable_raw_value)
    priors["osm_lanes_estimated"] = priors["lanes_estimated"].fillna(True).astype(bool)
    priors["resolved_lanes"] = pd.NA
    priors["lanes_source"] = pd.NA
    priors["lanes_confidence"] = pd.NA
    priors.loc[explicit_lane_mask, "resolved_lanes"] = priors.loc[
        explicit_lane_mask, "lanes"
    ].map(_positive_float)
    priors.loc[explicit_lane_mask, "lanes_source"] = "osm_explicit"
    priors.loc[explicit_lane_mask, "lanes_confidence"] = "medium"

    neighbor_values = _corridor_neighbor_values(priors, "resolved_lanes", max_range=1.0)
    neighbor_mask = priors["resolved_lanes"].isna() & neighbor_values.notna()
    priors.loc[neighbor_mask, "resolved_lanes"] = neighbor_values.loc[neighbor_mask]
    priors.loc[neighbor_mask, "lanes_source"] = "same_corridor_neighbor"
    priors.loc[neighbor_mask, "lanes_confidence"] = "low"

    default_mask = priors["resolved_lanes"].isna()
    priors.loc[default_mask, "resolved_lanes"] = priors.loc[
        default_mask, "road_class"
    ].map(ROAD_CLASS_LANES).fillna(1)
    priors.loc[default_mask, "lanes_source"] = "road_class_default"
    priors.loc[default_mask, "lanes_confidence"] = "low"
    priors["resolved_lanes"] = pd.to_numeric(priors["resolved_lanes"]).clip(lower=1)

    per_lane = priors["road_class"].map(CAPACITY_PER_LANE_VPH).fillna(700.0)
    priors["estimated_capacity_vph"] = priors["resolved_lanes"] * per_lane
    priors["capacity_source"] = "road_class_x_resolved_lanes"
    priors["capacity_confidence"] = priors["lanes_confidence"].map(
        {"medium": "medium", "low": "low"}
    ).fillna("low")


def _corridor_neighbor_values(
    priors: gpd.GeoDataFrame, value_column: str, *, max_range: float = 16.2
) -> pd.Series:
    centroids = priors.geometry.centroid
    tiles_x = (centroids.x // 1000).astype(int)
    tiles_y = (centroids.y // 1000).astype(int)
    corridor = priors["edge_ref_normalized"].where(
        priors["edge_ref_normalized"] != "", priors["edge_name_normalized"]
    )
    frame = pd.DataFrame(
        {
            "corridor": corridor,
            "road_class": priors["road_class"],
            "tile_x": tiles_x,
            "tile_y": tiles_y,
            "value": pd.to_numeric(priors[value_column], errors="coerce"),
        },
        index=priors.index,
    )
    known = frame.loc[(frame["corridor"] != "") & frame["value"].notna()]
    stats = known.groupby(
        ["corridor", "road_class", "tile_x", "tile_y"], dropna=False
    )["value"].agg(["median", "count", "min", "max"])
    eligible = stats.loc[(stats["count"] >= 2) & ((stats["max"] - stats["min"]) <= max_range)]
    keys = pd.MultiIndex.from_frame(frame[["corridor", "road_class", "tile_x", "tile_y"]])
    return pd.Series(keys.map(eligible["median"]), index=priors.index, dtype="float64")


def _attach_controls(
    priors: gpd.GeoDataFrame,
    nodes: gpd.GeoDataFrame,
    campaign_directory: Path,
    log: Callable[[str], None],
) -> None:
    node_controls = {
        _node_id(row.osmid): _primary_value(row.highway)
        for row in nodes.itertuples(index=False)
        if _primary_value(row.highway) in {"traffic_signals", "stop", "mini_roundabout"}
    }
    downstream_node_ids = priors["v"].map(_node_id)
    priors["downstream_node_id"] = downstream_node_ids
    priors["downstream_control_type"] = downstream_node_ids.map(node_controls).fillna("none")
    priors["control_source"] = priors["downstream_control_type"].map(
        lambda value: "osm_node" if value != "none" else "none"
    )
    priors["control_confidence"] = priors["downstream_control_type"].map(
        lambda value: "medium" if value != "none" else "not_applicable"
    )

    signal_points = []
    signal_points.extend(
        _load_points(campaign_directory / "pbot_traffic_signals", lambda _p: True)
    )
    signal_points.extend(
        _load_points(
            campaign_directory / "clark_county_signals",
            lambda properties: str(properties.get("Type") or "").strip()
            == "Traffic Signal",
        )
    )
    signal_nodes = _nearest_node_ids(signal_points, nodes, SIGNAL_NODE_DISTANCE_M)
    official_signal_mask = downstream_node_ids.isin(signal_nodes)
    priors.loc[official_signal_mask, "downstream_control_type"] = "traffic_signals"
    priors.loc[official_signal_mask, "control_source"] = "official_inventory"
    priors.loc[official_signal_mask, "control_confidence"] = "high"
    priors["control_timing_available"] = False

    clark_stops = _load_points(
        campaign_directory / "clark_county_signs",
        lambda properties: str(properties.get("SIGNTYPE") or "").strip() == "Stop",
    )
    stop_node_counts = Counter(
        _nearest_node_ids(clark_stops, nodes, STOP_NODE_DISTANCE_M, retain_duplicates=True)
    )
    priors["official_stop_signs_nearby"] = (
        downstream_node_ids.map(stop_node_counts).fillna(0).astype(int)
    )
    priors["stop_directionality"] = priors["official_stop_signs_nearby"].map(
        lambda count: "unresolved" if count else "not_applicable"
    )
    log(
        f"attached {len(signal_nodes):,} official signal-node matches and "
        f"{sum(stop_node_counts.values()):,} Clark stop-sign proximity matches"
    )


def _attach_traffic_evidence(
    priors: gpd.GeoDataFrame,
    profiles_path: Path | None,
    log: Callable[[str], None],
) -> None:
    for prefix in ("am", "pm"):
        priors[f"historical_{prefix}_available"] = False
        priors[f"historical_{prefix}_observations"] = 0
        priors[f"historical_{prefix}_speed_kph"] = pd.NA
        priors[f"historical_{prefix}_volume_vph"] = pd.NA
    if profiles_path is None or not profiles_path.exists():
        priors["traffic_evidence"] = "unobserved"
        log("no compact traffic profile supplied; marked all edges unobserved")
        return
    profiles = pd.read_parquet(profiles_path)
    required = {"edge_id", "period", "speed_kph", "volume", "observation_count"}
    missing = sorted(required - set(profiles.columns))
    if missing:
        raise EdgePriorBuildError("Traffic profiles missing fields: " + ", ".join(missing))
    profiles = profiles.copy()
    profiles["weighted_speed"] = profiles["speed_kph"] * profiles["observation_count"]
    profiles["weighted_volume"] = profiles["volume"] * profiles["observation_count"]
    grouped = profiles.groupby(["edge_id", "period"], sort=False).agg(
        observations=("observation_count", "sum"),
        weighted_speed=("weighted_speed", "sum"),
        weighted_volume=("weighted_volume", "sum"),
    )
    grouped["speed_kph"] = grouped["weighted_speed"] / grouped["observations"]
    grouped["volume_vph"] = grouped["weighted_volume"] / grouped["observations"]
    period_map = {
        "weekday_morning": "am",
        "weekday_afternoon": "pm",
    }
    for period, prefix in period_map.items():
        if period not in grouped.index.get_level_values("period"):
            continue
        values = grouped.xs(period, level="period")
        edge_ids = priors["edge_id"]
        priors[f"historical_{prefix}_observations"] = (
            edge_ids.map(values["observations"]).fillna(0).astype(int)
        )
        priors[f"historical_{prefix}_speed_kph"] = edge_ids.map(values["speed_kph"])
        priors[f"historical_{prefix}_volume_vph"] = edge_ids.map(values["volume_vph"])
        priors[f"historical_{prefix}_available"] = (
            priors[f"historical_{prefix}_observations"] > 0
        )
    priors["traffic_evidence"] = (
        priors["historical_am_available"] | priors["historical_pm_available"]
    ).map({True: "historical_observed", False: "unobserved"})
    log(
        f"attached historical evidence to "
        f"{int((priors['traffic_evidence'] == 'historical_observed').sum()):,} edges"
    )


def _build_report(
    priors: gpd.GeoDataFrame,
    review: pd.DataFrame,
    source_counts: dict[str, int],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    total = len(priors)

    def counts(column: str) -> dict[str, int]:
        return {
            str(key): int(value)
            for key, value in priors[column].fillna("missing").value_counts().items()
        }

    unresolved_stop_edges = int((priors["stop_directionality"] == "unresolved").sum())
    return {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "graph_version": str(priors["graph_version"].iloc[0]) if total else "",
        "authority_campaign_id": str(
            manifest.get("campaignId") or priors["authority_campaign_id"].iloc[0]
        ),
        "directed_edges": total,
        "authority_source_features": source_counts,
        "speed_source_counts": counts("speed_source"),
        "speed_confidence_counts": counts("speed_confidence"),
        "lane_source_counts": counts("lanes_source"),
        "control_type_counts": counts("downstream_control_type"),
        "control_source_counts": counts("control_source"),
        "traffic_evidence_counts": counts("traffic_evidence"),
        "authority_review_rows": len(review),
        "historically_observed_edge_percent": round(
            100.0 * int((priors["traffic_evidence"] == "historical_observed").sum()) / total,
            4,
        )
        if total
        else 0.0,
        "unresolved_stop_direction_edges": unresolved_stop_edges,
        "limitations": [
            "Authority line matches are spatial/orientation/name inferences and require review when ambiguous.",
            "Clark County NumThruLanes is retained as a total-roadway evidence field and does not override directed lane counts.",
            "Clark County stop-sign proximity does not establish which approach is controlled; directionality remains unresolved.",
            "PBOT regulatory sign codes are not promoted to stop controls because the local snapshot does not define the code semantics.",
            "Signal inventories establish location only; cycle, phase, offset, and pedestrian timing are unavailable.",
            "Road-class and same-corridor values are modeled priors, not legal or observed measurements.",
            "Unobserved edges do not yet receive synthetic background volume; demand assignment is the next modeling gate.",
        ],
    }


def report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Directed edge prior quality report",
        "",
        f"- Graph: `{report['graph_version']}`",
        f"- Directed edges: {report['directed_edges']:,}",
        f"- Authority review rows: {report['authority_review_rows']:,}",
        f"- Historically observed edges: {report['historically_observed_edge_percent']:.4f}%",
        f"- Edges near official stops with unresolved approach direction: {report['unresolved_stop_direction_edges']:,}",
        "",
    ]
    for heading, key in (
        ("Speed evidence", "speed_source_counts"),
        ("Lane evidence", "lane_source_counts"),
        ("Downstream controls", "control_type_counts"),
        ("Traffic evidence", "traffic_evidence_counts"),
    ):
        lines.extend([f"## {heading}", "", "| Value | Directed edges |", "| --- | ---: |"])
        lines.extend(
            f"| `{name}` | {count:,} |"
            for name, count in report[key].items()
        )
        lines.append("")
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def _load_points(
    directory: Path, predicate: Callable[[dict[str, Any]], bool]
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for path in sorted(directory.glob("*.geojson")):
        document = json.loads(path.read_text())
        for feature in document.get("features", []):
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates")
            if (
                predicate(properties)
                and geometry.get("type") == "Point"
                and isinstance(coordinates, list)
                and len(coordinates) >= 2
            ):
                points.append((float(coordinates[0]), float(coordinates[1])))
    return points


def _nearest_node_ids(
    coordinates: Iterable[tuple[float, float]],
    nodes: gpd.GeoDataFrame,
    max_distance_m: float,
    *,
    retain_duplicates: bool = False,
) -> list[str]:
    coordinate_list = list(coordinates)
    if not coordinate_list:
        return []
    points = gpd.GeoDataFrame(
        {"point_id": range(len(coordinate_list))},
        geometry=gpd.points_from_xy(
            [value[0] for value in coordinate_list],
            [value[1] for value in coordinate_list],
        ),
        crs="EPSG:4326",
    ).to_crs(PROJECTED_CRS)
    joined = gpd.sjoin_nearest(
        points,
        nodes[["osmid", "geometry"]],
        how="left",
        max_distance=max_distance_m,
        distance_col="distance_m",
    )
    values = [_node_id(value) for value in joined["osmid"].dropna()]
    return values if retain_duplicates else sorted(set(values))


def _feature_line(geometry: Any, geometry_format: str) -> LineString | None:
    if not isinstance(geometry, dict):
        return None
    if geometry_format == "esri_json":
        paths = geometry.get("paths")
        if not isinstance(paths, list) or not paths:
            return None
        coordinates = max(paths, key=len)
    else:
        if geometry.get("type") != "LineString":
            return None
        coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        return LineString([(float(value[0]), float(value[1])) for value in coordinates])
    except (TypeError, ValueError, IndexError):
        return None


def _parse_osm_speed_kph(value: Any) -> float | None:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    parsed: list[float] = []
    for item in values:
        if item is None or (isinstance(item, float) and math.isnan(item)):
            continue
        text = str(item).strip().lower()
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            continue
        speed = float(match.group(1))
        if "mph" in text:
            speed *= MPH_TO_KPH
        if 5.0 <= speed <= 160.0:
            parsed.append(speed)
    return min(parsed) if parsed else None


def _normalize_road_name(value: Any) -> str:
    text = _primary_value(value).upper()
    text = re.sub(r"\b(NORTH|SOUTH|EAST|WEST|NORTHEAST|NORTHWEST|SOUTHEAST|SOUTHWEST)\b", " ", text)
    text = re.sub(r"\b(N|S|E|W|NE|NW|SE|SW)\b", " ", text)
    replacements = {
        "AVENUE": "AVE",
        "STREET": "ST",
        "ROAD": "RD",
        "BOULEVARD": "BLVD",
        "HIGHWAY": "HWY",
        "DRIVE": "DR",
        "LANE": "LN",
        "PARKWAY": "PKWY",
    }
    for old, new in replacements.items():
        text = re.sub(rf"\b{old}\b", new, text)
    return " ".join(re.findall(r"[A-Z0-9]+", text))


def _normalize_ref(value: Any) -> str:
    text = _primary_value(value).upper()
    match = re.search(r"\b(?:I|US|OR|SR|WA)[ -]?(\d+)\b", text)
    return match.group(0).replace(" ", "-") if match else ""


def _names_agree(edge_name: str, edge_ref: str, source_name: str, source_ref: str) -> bool:
    if edge_ref and source_ref and edge_ref == source_ref:
        return True
    if not edge_name or not source_name:
        return False
    if edge_name == source_name:
        return True
    edge_tokens = set(edge_name.split())
    source_tokens = set(source_name.split())
    meaningful = {"ST", "AVE", "RD", "BLVD", "HWY", "DR", "LN", "PKWY"}
    shared = (edge_tokens & source_tokens) - meaningful
    return bool(shared) and len(shared) >= min(2, len(edge_tokens - meaningful))


def _line_bearing(geometry: Any) -> float:
    if geometry is None or geometry.is_empty:
        return 0.0
    coordinates = list(geometry.coords)
    start = coordinates[0]
    end = coordinates[-1]
    return (math.degrees(math.atan2(end[0] - start[0], end[1] - start[1])) + 360.0) % 360.0


def _undirected_angle_difference(first: float, second: float) -> float:
    difference = abs(first - second) % 180.0
    return min(difference, 180.0 - difference)


def _primary_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_raw_value(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (list, tuple, set)):
        return json.dumps([str(item) for item in value], separators=(",", ":"))
    return str(value)


def _node_id(value: Any) -> str:
    """Canonicalize IDs that GeoPandas may coerce from integer to float."""

    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)
