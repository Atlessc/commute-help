"""Projected snapping against the locally loaded directed road graph."""

from dataclasses import dataclass
import json
import math
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point

from backend.app.schemas.routing import (
    ClosureDirectionCandidate,
    ClosureRoadSelectionResponse,
    DirectedEdgeReference,
    GeoJSONLineString,
    SnappedLocation,
)
from backend.app.services.graph_service import GraphService, GraphUnavailableError


@dataclass(frozen=True)
class SpatialSettings:
    """Safety limits for interactive map snapping."""

    maximum_snap_distance_m: float = 2_000


class SpatialService:
    """Resolve map coordinates without sending the regional graph to browsers."""

    def __init__(
        self,
        graph_service: GraphService,
        settings: SpatialSettings | None = None,
    ) -> None:
        self.graph_service = graph_service
        self.settings = settings or SpatialSettings()
        self._to_projected = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)
        self._to_wgs84 = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)

    def select_road(self, latitude: float, longitude: float) -> SnappedLocation:
        """Snap a WGS84 click to the nearest edge and routable node."""

        projected_nodes = self.graph_service.projected_nodes
        projected_edges = self.graph_service.projected_edges
        edges = self.graph_service.edges
        manifest = self.graph_service.manifest
        if (
            not self.graph_service.ready
            or projected_nodes is None
            or projected_edges is None
            or edges is None
            or manifest is None
        ):
            raise GraphUnavailableError("Routing graph has not been loaded.")

        x, y = self._to_projected.transform(longitude, latitude)
        point = Point(x, y)
        edge_position, edge_distance = self._nearest_position(projected_edges, point)
        node_position, node_distance = self._nearest_position(projected_nodes, point)
        if edge_distance > self.settings.maximum_snap_distance_m:
            raise NoNearbyRoadError(edge_distance)

        projected_edge = projected_edges.iloc[edge_position]
        snapped_point = projected_edge.geometry.interpolate(
            projected_edge.geometry.project(point)
        )
        snapped_lng, snapped_lat = self._to_wgs84.transform(
            snapped_point.x,
            snapped_point.y,
        )

        edge_index = projected_edges.index[edge_position]
        edge_data = edges.loc[edge_index]
        node_id = projected_nodes.index[node_position]
        u, v, key = edge_index
        return SnappedLocation(
            lat=snapped_lat,
            lng=snapped_lng,
            node_id=str(node_id),
            distance_m=round(edge_distance, 1),
            label=str(edge_data.get("road_name", "Unnamed road")),
            edge=DirectedEdgeReference(
                edge_id=str(edge_data["edge_id"]),
                u=str(u),
                v=str(v),
                key=int(key),
                road_name=str(edge_data.get("road_name", "Unnamed road")),
                road_class=str(edge_data.get("road_class", "unclassified")),
                osm_way_ids=_osm_way_ids(edge_data.get("osm_way_ids")),
                geometry_fingerprint=str(edge_data.get("geometry_fingerprint", "")),
                lanes=float(edge_data.get("lanes", 1)),
                maxspeed_kph=float(edge_data.get("maxspeed_kph", 35)),
            ),
        )

    def select_closure_road(
        self,
        latitude: float,
        longitude: float,
    ) -> ClosureRoadSelectionResponse:
        """Return independently selectable directions for the nearest road segment."""

        graph = self.graph_service.graph
        projected_edges = self.graph_service.projected_edges
        edges = self.graph_service.edges
        manifest = self.graph_service.manifest
        if (
            not self.graph_service.ready
            or graph is None
            or projected_edges is None
            or edges is None
            or manifest is None
        ):
            raise GraphUnavailableError("Routing graph has not been loaded.")

        x, y = self._to_projected.transform(longitude, latitude)
        point = Point(x, y)
        edge_position, edge_distance = self._nearest_position(projected_edges, point)
        if edge_distance > self.settings.maximum_snap_distance_m:
            raise NoNearbyRoadError(edge_distance)

        selected_index = projected_edges.index[edge_position]
        selected_data = edges.loc[selected_index]
        selected_u, selected_v, selected_key = selected_index
        fingerprint = str(selected_data["geometry_fingerprint"])
        candidates: list[ClosureDirectionCandidate] = []
        seen_edge_ids: set[str] = set()

        for u, v in ((selected_u, selected_v), (selected_v, selected_u)):
            for key, edge_data in (graph.get_edge_data(u, v) or {}).items():
                edge_id = str(edge_data["edge_id"])
                if (
                    str(edge_data.get("geometry_fingerprint")) != fingerprint
                    or edge_id in seen_edge_ids
                ):
                    continue
                seen_edge_ids.add(edge_id)
                geometry = edge_data["geometry"]
                coordinates = [(float(px), float(py)) for px, py in geometry.coords]
                start = (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"]))
                if _squared_distance(coordinates[-1], start) < _squared_distance(
                    coordinates[0], start
                ):
                    coordinates.reverse()
                candidates.append(
                    ClosureDirectionCandidate(
                        edge=DirectedEdgeReference(
                            edge_id=edge_id,
                            u=str(u),
                            v=str(v),
                            key=int(key),
                            road_name=str(edge_data.get("road_name", "Unnamed road")),
                            road_class=str(
                                edge_data.get("road_class", "unclassified")
                            ),
                            osm_way_ids=_osm_way_ids(edge_data.get("osm_way_ids")),
                            geometry_fingerprint=str(
                                edge_data.get("geometry_fingerprint", "")
                            ),
                            lanes=float(edge_data.get("lanes", 1)),
                            maxspeed_kph=float(edge_data.get("maxspeed_kph", 35)),
                        ),
                        direction_label=_direction_label(coordinates),
                        geometry=GeoJSONLineString(coordinates=coordinates),
                    )
                )

        if not candidates:
            raise NoNearbyRoadError(edge_distance)
        candidates.sort(key=lambda candidate: candidate.direction_label)
        return ClosureRoadSelectionResponse(
            graph_version=manifest.graph_version,
            road_name=str(selected_data.get("road_name", "Unnamed road")),
            distance_m=round(edge_distance, 1),
            selected_edge_id=str(graph.edges[selected_u, selected_v, selected_key]["edge_id"]),
            directions=candidates,
        )

    def resolve_node(
        self,
        latitude: float,
        longitude: float,
        node_id: str | None,
    ) -> Any:
        """Resolve a retained snap or find the nearest routable graph node."""

        if node_id is not None:
            resolved = self.graph_service.node_lookup.get(node_id)
            if resolved is None:
                raise InvalidGraphNodeError(node_id)
            return resolved

        projected_nodes = self.graph_service.projected_nodes
        if not self.graph_service.ready or projected_nodes is None:
            raise GraphUnavailableError("Routing graph has not been loaded.")
        x, y = self._to_projected.transform(longitude, latitude)
        position, distance = self._nearest_position(projected_nodes, Point(x, y))
        if distance > self.settings.maximum_snap_distance_m:
            raise NoNearbyRoadError(distance)
        return projected_nodes.index[position]

    @staticmethod
    def _nearest_position(frame: Any, point: Point) -> tuple[int, float]:
        indices, distances = frame.sindex.nearest(
            point,
            return_all=False,
            return_distance=True,
        )
        if not len(distances):
            raise NoNearbyRoadError(float("inf"))
        return int(indices[1][0]), float(distances[0])


class NoNearbyRoadError(ValueError):
    """No routable road falls within the interactive snapping limit."""

    def __init__(self, distance_m: float) -> None:
        self.distance_m = distance_m
        super().__init__("No routable road is close enough to that point.")


class InvalidGraphNodeError(ValueError):
    """A retained selection refers to a node outside the loaded graph."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__("The selected point no longer matches the loaded road graph.")


def _squared_distance(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _osm_way_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [] if value is None else [str(value)]


def _direction_label(coordinates: list[tuple[float, float]]) -> str:
    """Describe edge direction without relying on color alone."""

    start_lng, start_lat = coordinates[0]
    end_lng, end_lat = coordinates[-1]
    average_latitude = math.radians((start_lat + end_lat) / 2)
    east = (end_lng - start_lng) * math.cos(average_latitude)
    north = end_lat - start_lat
    bearing = (math.degrees(math.atan2(east, north)) + 360) % 360
    labels = (
        "Northbound",
        "Northeastbound",
        "Eastbound",
        "Southeastbound",
        "Southbound",
        "Southwestbound",
        "Westbound",
        "Northwestbound",
    )
    return labels[int((bearing + 22.5) // 45) % 8]
