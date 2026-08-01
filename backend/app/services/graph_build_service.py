"""Reproducible OpenStreetMap graph construction and integrity validation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import geopandas as gpd
import networkx as nx
import osmnx as ox
from pandas.api.types import is_object_dtype, is_string_dtype
from shapely.geometry import LineString, shape
from shapely.geometry.base import BaseGeometry

from backend.app.schemas.graph import (
    GraphArtifact,
    GraphManifest,
    GraphMetrics,
    GraphRegion,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
BUILD_TOOL_VERSION = "commute-help-graph-builder-v1"

HIGHWAY_SPEEDS_KPH: dict[str, float] = {
    "motorway": 100,
    "motorway_link": 70,
    "trunk": 80,
    "trunk_link": 60,
    "primary": 60,
    "primary_link": 50,
    "secondary": 50,
    "secondary_link": 45,
    "tertiary": 45,
    "tertiary_link": 40,
    "unclassified": 35,
    "residential": 30,
    "living_street": 15,
    "service": 20,
    "track": 15,
}

DEFAULT_LANES: dict[str, float] = {
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

CAPACITY_PER_LANE_VPH: dict[str, float] = {
    "motorway": 2000,
    "motorway_link": 1600,
    "trunk": 1800,
    "trunk_link": 1500,
    "primary": 1600,
    "primary_link": 1300,
    "secondary": 1400,
    "secondary_link": 1200,
    "tertiary": 1200,
    "tertiary_link": 1000,
    "unclassified": 900,
    "residential": 700,
    "living_street": 400,
    "service": 400,
    "track": 250,
}


@dataclass(frozen=True)
class ValidationRoute:
    """Public landmark pair used only for repeatable graph integrity checks."""

    name: str
    origin: tuple[float, float]
    destination: tuple[float, float]


@dataclass(frozen=True)
class RegionDefinition:
    """Parsed versioned build region and its acceptance routes."""

    id: str
    name: str
    geometry: BaseGeometry
    validation_routes: tuple[ValidationRoute, ...]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        west, south, east, north = self.geometry.bounds
        return west, south, east, north


def load_region(region_path: Path) -> RegionDefinition:
    """Load one polygon feature from the committed region definition."""

    payload = json.loads(region_path.read_text(encoding="utf-8"))
    features = payload.get("features", [])
    if len(features) != 1:
        raise ValueError("Region definition must contain exactly one feature")

    feature = features[0]
    properties = feature.get("properties", {})
    geometry = shape(feature["geometry"])
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or not geometry.is_valid:
        raise ValueError("Region geometry must be a valid polygon or multipolygon")

    validation_routes = tuple(
        ValidationRoute(
            name=item["name"],
            origin=tuple(item["origin"]),
            destination=tuple(item["destination"]),
        )
        for item in properties.get("validationRoutes", [])
    )
    if not validation_routes:
        raise ValueError("Region definition must include validation routes")

    return RegionDefinition(
        id=properties["id"],
        name=properties["name"],
        geometry=geometry,
        validation_routes=validation_routes,
    )


def download_drive_graph(region: RegionDefinition, cache_dir: Path) -> nx.MultiDiGraph:
    """Download the complete OSMnx drive network inside the fixed region."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = cache_dir
    ox.settings.requests_timeout = 300
    return ox.graph.graph_from_polygon(
        region.geometry,
        network_type="drive",
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
    )


def normalize_graph(graph: nx.MultiDiGraph, graph_version: str) -> nx.MultiDiGraph:
    """Add stable identity, travel time, and explicit routing defaults."""

    graph.graph["graph_version"] = graph_version
    graph.graph["build_tool"] = BUILD_TOOL_VERSION
    ox.routing.add_edge_speeds(
        graph,
        hwy_speeds=HIGHWAY_SPEEDS_KPH,
        fallback=35,
    )
    ox.routing.add_edge_travel_times(graph)

    for u, v, key, data in graph.edges(keys=True, data=True):
        road_class = _first_string(data.get("highway"), "unclassified")
        raw_lanes = data.get("lanes")
        lanes = _parse_positive_number(raw_lanes)
        lanes_estimated = lanes is None
        if lanes is None:
            lanes = DEFAULT_LANES.get(road_class, 1)

        raw_name = data.get("name")
        road_name = _joined_string(raw_name, "Unnamed road")
        geometry = data.get("geometry")
        if geometry is None:
            geometry = LineString(
                [
                    (graph.nodes[u]["x"], graph.nodes[u]["y"]),
                    (graph.nodes[v]["x"], graph.nodes[v]["y"]),
                ]
            )
            data["geometry"] = geometry

        geometry_fingerprint = hashlib.sha256(geometry.normalize().wkb).hexdigest()
        osm_way_ids = _osm_way_ids(data.get("osmid"))
        identity = json.dumps(
            [graph_version, str(u), str(v), key, osm_way_ids, geometry_fingerprint],
            separators=(",", ":"),
        )

        speed_kph = float(data["speed_kph"])
        length_m = float(data["length"])
        free_flow_seconds = float(data["travel_time"])
        capacity_per_lane = CAPACITY_PER_LANE_VPH.get(road_class, 800)

        data.update(
            {
                "edge_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "osm_way_ids": json.dumps(osm_way_ids, separators=(",", ":")),
                "geometry_fingerprint": geometry_fingerprint,
                "road_name": road_name,
                "road_name_source_missing": raw_name in (None, ""),
                "road_class": road_class,
                "lanes": float(lanes),
                "lanes_estimated": lanes_estimated,
                "maxspeed_kph": speed_kph,
                "free_flow_seconds": free_flow_seconds,
                "estimated_capacity_vph": float(lanes) * capacity_per_lane,
                "service_penalty": 1.3 if road_class == "service" else 1.0,
                "residential_penalty": 1.15 if road_class == "residential" else 1.0,
                "surface_penalty": _surface_penalty(data.get("surface")),
                "length_m": length_m,
            }
        )

    return graph


def validate_graph(
    graph: nx.MultiDiGraph,
    region: RegionDefinition,
    graph_version: str,
) -> dict[str, Any]:
    """Measure graph integrity and evaluate the Phase 1 acceptance gate."""

    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    weak_components = list(nx.weakly_connected_components(graph))
    strong_component_count = nx.number_strongly_connected_components(graph)
    largest_weak_nodes = max((len(component) for component in weak_components), default=0)
    largest_weak_percent = (
        round(100 * largest_weak_nodes / node_count, 3) if node_count else 0.0
    )

    invalid_geometry = 0
    zero_length = 0
    extreme_length = 0
    missing_speed = 0
    estimated_lanes = 0
    unnamed_roads = 0
    one_way_edges = 0
    bridge_edges = 0
    tunnel_edges = 0

    for _, _, _, data in graph.edges(keys=True, data=True):
        geometry = data.get("geometry")
        if geometry is None or not geometry.is_valid:
            invalid_geometry += 1
        length_m = _parse_positive_number(data.get("length_m", data.get("length")))
        if length_m is None:
            zero_length += 1
        elif length_m > 50_000:
            extreme_length += 1
        if _parse_positive_number(data.get("maxspeed_kph", data.get("speed_kph"))) is None:
            missing_speed += 1
        if _truthy(data.get("lanes_estimated")):
            estimated_lanes += 1
        if _truthy(data.get("road_name_source_missing")):
            unnamed_roads += 1
        if _truthy(data.get("oneway")):
            one_way_edges += 1
        if _tag_present(data.get("bridge")):
            bridge_edges += 1
        if _tag_present(data.get("tunnel")):
            tunnel_edges += 1

    undirected = graph.to_undirected(as_view=True)
    isolated_nodes = nx.number_of_isolates(undirected)
    dead_end_nodes = sum(1 for _, degree in undirected.degree() if degree == 1)
    route_results = [_validate_route(graph, route) for route in region.validation_routes]

    failures: list[str] = []
    if not node_count or not edge_count:
        failures.append("Graph is empty.")
    if largest_weak_percent < 95:
        failures.append("Largest weak component contains less than 95% of nodes.")
    if invalid_geometry:
        failures.append("One or more edges have missing or invalid geometry.")
    if zero_length:
        failures.append("One or more edges have missing or non-positive length.")
    if missing_speed:
        failures.append("One or more edges have missing or non-positive speed.")
    if any(not result["success"] for result in route_results):
        failures.append("One or more representative regional routes failed.")

    return {
        "schema_version": 1,
        "graph_version": graph_version,
        "generated_at": datetime.now(PACIFIC).isoformat(),
        "gate_passed": not failures,
        "gate_failures": failures,
        "topology": {
            "nodes": node_count,
            "directed_edges": edge_count,
            "weakly_connected_components": len(weak_components),
            "strongly_connected_components": strong_component_count,
            "largest_weak_component_nodes": largest_weak_nodes,
            "largest_weak_component_percent": largest_weak_percent,
            "isolated_nodes": isolated_nodes,
            "dead_end_nodes": dead_end_nodes,
            "one_way_edges": one_way_edges,
            "bridge_edges": bridge_edges,
            "tunnel_edges": tunnel_edges,
        },
        "data_quality": {
            "invalid_or_missing_geometry_edges": invalid_geometry,
            "zero_or_missing_length_edges": zero_length,
            "extreme_length_edges": extreme_length,
            "missing_speed_edges": missing_speed,
            "estimated_lane_edges": estimated_lanes,
            "unnamed_road_edges": unnamed_roads,
        },
        "representative_routes": route_results,
    }


def write_graph_artifacts(
    graph: nx.MultiDiGraph,
    region: RegionDefinition,
    graph_version: str,
    output_dir: Path,
    validation_report: dict[str, Any],
) -> GraphManifest:
    """Write GraphML, GeoParquet tables, validation, and a checksum manifest."""

    output_dir.mkdir(parents=True, exist_ok=True)
    graph_path = output_dir / "portland-vancouver.graphml"
    nodes_path = output_dir / "nodes.parquet"
    edges_path = output_dir / "edges.parquet"
    report_path = output_dir / "validation-report.json"

    ox.io.save_graphml(graph, graph_path)
    nodes, edges = ox.convert.graph_to_gdfs(graph, fill_edge_geometry=True)
    _parquet_safe(nodes.reset_index()).to_parquet(nodes_path, index=False)
    _parquet_safe(edges.reset_index()).to_parquet(edges_path, index=False)
    report_path.write_text(
        json.dumps(validation_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    topology = validation_report["topology"]
    artifacts = {
        "graphml": _artifact(graph_path),
        "nodes_parquet": _artifact(nodes_path),
        "edges_parquet": _artifact(edges_path),
        "validation_report": _artifact(report_path),
    }
    manifest = GraphManifest(
        schema_version=1,
        graph_version=graph_version,
        source="OpenStreetMap via OSMnx Overpass download",
        built_at=datetime.now(PACIFIC).isoformat(),
        region=GraphRegion(id=region.id, name=region.name, bounds=region.bounds),
        network_type="drive",
        build_tool=f"{BUILD_TOOL_VERSION} with OSMnx {ox.__version__}",
        validation_passed=bool(validation_report["gate_passed"]),
        metrics=GraphMetrics(
            nodes=topology["nodes"],
            directed_edges=topology["directed_edges"],
            weakly_connected_components=topology["weakly_connected_components"],
            largest_weak_component_percent=topology["largest_weak_component_percent"],
        ),
        artifacts=artifacts,
    )
    (output_dir / "graph-manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def refresh_manifest_validation(
    report: dict[str, Any],
    report_path: Path,
    manifest_path: Path,
) -> GraphManifest:
    """Keep validation state, counts, and report checksum internally consistent."""

    manifest = GraphManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if manifest.graph_version != report["graph_version"]:
        raise ValueError("Validation report graph version does not match the manifest")

    topology = report["topology"]
    manifest.validation_passed = bool(report["gate_passed"])
    manifest.metrics = GraphMetrics(
        nodes=topology["nodes"],
        directed_edges=topology["directed_edges"],
        weakly_connected_components=topology["weakly_connected_components"],
        largest_weak_component_percent=topology["largest_weak_component_percent"],
    )
    manifest.artifacts["validation_report"] = _artifact(report_path)
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _validate_route(graph: nx.MultiDiGraph, route: ValidationRoute) -> dict[str, Any]:
    try:
        origin_node = _nearest_node(graph, route.origin)
        destination_node = _nearest_node(graph, route.destination)
        travel_time_seconds = nx.shortest_path_length(
            graph,
            origin_node,
            destination_node,
            weight="travel_time",
        )
        return {
            "name": route.name,
            "success": True,
            "travel_time_seconds": round(float(travel_time_seconds), 1),
        }
    except (nx.NetworkXNoPath, nx.NodeNotFound, ValueError) as error:
        return {"name": route.name, "success": False, "reason": type(error).__name__}


def _nearest_node(
    graph: nx.MultiDiGraph,
    coordinates: tuple[float, float],
) -> Any:
    """Find the nearest node by haversine distance without optional packages."""

    longitude, latitude = coordinates
    latitude_radians = math.radians(latitude)

    def distance(node: Any) -> float:
        node_data = graph.nodes[node]
        node_latitude = math.radians(float(node_data["y"]))
        latitude_delta = node_latitude - latitude_radians
        longitude_delta = math.radians(float(node_data["x"]) - longitude)
        haversine = (
            math.sin(latitude_delta / 2) ** 2
            + math.cos(latitude_radians)
            * math.cos(node_latitude)
            * math.sin(longitude_delta / 2) ** 2
        )
        return haversine

    try:
        return min(graph.nodes, key=distance)
    except ValueError as error:
        raise ValueError("Cannot snap a validation route against an empty graph") from error


def _first_string(value: Any, fallback: str) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else fallback
    return str(value) if value not in (None, "") else fallback


def _joined_string(value: Any, fallback: str) -> str:
    if isinstance(value, list):
        return " / ".join(str(item) for item in value)
    return str(value) if value not in (None, "") else fallback


def _parse_positive_number(value: Any) -> float | None:
    values = value if isinstance(value, list) else [value]
    parsed: list[float] = []
    for item in values:
        if item is None:
            continue
        try:
            number = float(str(item).split(";")[0].strip())
        except ValueError:
            continue
        if math.isfinite(number) and number > 0:
            parsed.append(number)
    return max(parsed) if parsed else None


def _osm_way_ids(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return sorted({str(item) for item in values if item is not None})


def _surface_penalty(value: Any) -> float:
    surface = _first_string(value, "unknown")
    if surface in {"unpaved", "gravel", "dirt", "ground", "fine_gravel"}:
        return 1.35
    return 1.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "yes", "1"}


def _tag_present(value: Any) -> bool:
    if isinstance(value, list):
        return any(_tag_present(item) for item in value)
    return value not in (None, "", "no", "false", False)


def _parquet_safe(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    safe = frame.copy()
    geometry_name = safe.geometry.name
    for column in safe.columns:
        if column == geometry_name:
            continue
        if not (is_object_dtype(safe[column].dtype) or is_string_dtype(safe[column].dtype)):
            continue
        safe[column] = safe[column].map(
            lambda value: json.dumps(value, sort_keys=True)
            if isinstance(value, (list, dict, tuple, set))
            else str(value)
            if value is not None
            else None
        )
    return safe


def _artifact(path: Path) -> GraphArtifact:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return GraphArtifact(
        filename=path.name,
        sha256=digest.hexdigest(),
        size_bytes=path.stat().st_size,
    )
