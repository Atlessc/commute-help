"""Directed A* baseline routing over the immutable shared graph."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import networkx as nx

from backend.app.schemas.routing import (
    AlternativeRoute,
    GeoJSONLineString,
    GoogleMapsUrlRequest,
    GoogleMapsUrlResponse,
    RouteCompareRequest,
    RouteCompareResponse,
    RouteAlternativesResponse,
    RouteSummary,
)
from backend.app.services.graph_service import GraphService, GraphUnavailableError
from backend.app.services.spatial_service import SpatialService

MAX_HEURISTIC_SPEED_MPS = 140 / 3.6
EARTH_RADIUS_M = 6_371_009
PACIFIC = ZoneInfo("America/Los_Angeles")
WeightFunction = Callable[[Any, Any, dict[Any, dict[str, Any]]], float]


@dataclass(frozen=True)
class AppliedRestriction:
    """Validated dynamic cost for one immutable graph edge."""

    type: str
    remaining_lanes: float | None = None
    speed_limit_kph: float | None = None


@dataclass(frozen=True)
class RouteDetails:
    """Internal route facts used to reject and rank alternatives."""

    summary: RouteSummary
    edge_ids: frozenset[str]
    edge_lengths: dict[str, float]
    residential_distance_m: float
    structural_penalty_seconds: float


class RoutingService:
    """Calculate honest free-flow routes without mutating the shared graph."""

    def __init__(self, graph_service: GraphService, spatial_service: SpatialService) -> None:
        self.graph_service = graph_service
        self.spatial_service = spatial_service

    def compare(self, request: RouteCompareRequest) -> RouteCompareResponse:
        """Return a baseline and an optional directed-closure comparison."""

        graph = self.graph_service.graph
        manifest = self.graph_service.manifest
        if not self.graph_service.ready or graph is None or manifest is None:
            raise GraphUnavailableError("Routing graph has not been loaded.")

        origin = self.spatial_service.resolve_node(
            request.origin.lat,
            request.origin.lng,
            request.origin.node_id,
        )
        destination = self.spatial_service.resolve_node(
            request.destination.lat,
            request.destination.lng,
            request.destination.node_id,
        )
        if origin == destination:
            raise SameLocationError()

        baseline_path = self.find_path(graph, origin, destination)
        baseline = self._summarize_route(
            graph,
            baseline_path,
            manifest.graph_version,
            origin,
            destination,
        )
        departure_time = request.departure_time or datetime.now(PACIFIC)
        restrictions, inactive_edge_ids = self._validate_restrictions(
            graph,
            request,
            manifest.graph_version,
            departure_time,
        )
        scenario: RouteSummary | None = None
        scenario_status = "not_requested"
        if restrictions:
            filtered_graph = nx.subgraph_view(
                graph,
                filter_edge=lambda u, v, key: str(
                    graph.edges[u, v, key]["edge_id"]
                )
                not in restrictions
                or restrictions[str(graph.edges[u, v, key]["edge_id"])].type
                != "full",
            )
            try:
                scenario_path = self.find_path(
                    filtered_graph,
                    origin,
                    destination,
                    weight=self._restriction_weight(restrictions),
                )
            except NoRouteError:
                scenario_status = "no_route"
            else:
                scenario_status = "available"
                scenario = self._summarize_route(
                    filtered_graph,
                    scenario_path,
                    manifest.graph_version,
                    origin,
                    destination,
                    restrictions,
                )
        elif inactive_edge_ids:
            scenario_status = "inactive"

        modeled = any(
            restriction.type in {"lane", "speed"}
            for restriction in restrictions.values()
        )
        return RouteCompareResponse(
            graph_version=manifest.graph_version,
            evidence_level="modeled_uncalibrated" if modeled else "free_flow",
            baseline=baseline,
            scenario=scenario,
            scenario_status=scenario_status,
            evaluated_departure_time=departure_time,
            applied_restriction_edge_ids=sorted(restrictions),
            inactive_restriction_edge_ids=sorted(inactive_edge_ids),
            assumptions=[
                "Uses OpenStreetMap road access and direction data.",
                "Uses free-flow speeds and class-based defaults, not live traffic.",
                "Service, residential, and unpaved roads receive routing penalties.",
                "Full closures remove only the selected directed edges.",
                "Lane restrictions use an uncalibrated lanes-before/lanes-after cost multiplier.",
                "Speed restrictions use the selected temporary speed as the edge travel time.",
                "Schedules are active from their start time up to, but not including, their end time.",
            ],
        )

    def alternatives(self, request: RouteCompareRequest) -> RouteAlternativesResponse:
        """Generate up to three distinct, transparently ranked corridors."""

        graph = self.graph_service.graph
        manifest = self.graph_service.manifest
        if not self.graph_service.ready or graph is None or manifest is None:
            raise GraphUnavailableError("Routing graph has not been loaded.")

        origin = self.spatial_service.resolve_node(
            request.origin.lat, request.origin.lng, request.origin.node_id
        )
        destination = self.spatial_service.resolve_node(
            request.destination.lat,
            request.destination.lng,
            request.destination.node_id,
        )
        if origin == destination:
            raise SameLocationError()

        departure_time = request.departure_time or datetime.now(PACIFIC)
        restrictions, _ = self._validate_restrictions(
            graph, request, manifest.graph_version, departure_time
        )
        routable_graph = nx.subgraph_view(
            graph,
            filter_edge=lambda u, v, key: str(graph.edges[u, v, key]["edge_id"])
            not in restrictions
            or restrictions[str(graph.edges[u, v, key]["edge_id"])].type
            != "full",
        )
        base_weight = self._restriction_weight(restrictions)
        fastest_path = self.find_path(
            routable_graph, origin, destination, weight=base_weight
        )
        fastest = self._route_details(
            routable_graph,
            fastest_path,
            manifest.graph_version,
            origin,
            destination,
            restrictions,
        )
        candidates = [fastest]
        generated_route_ids = {fastest.summary.route_id}
        edge_usage = {edge_id: 1 for edge_id in fastest.edge_ids}

        for _ in range(14):
            if len(candidates) >= 6:
                break
            try:
                path = self.find_path(
                    routable_graph,
                    origin,
                    destination,
                    weight=self._alternative_weight(restrictions, edge_usage),
                )
            except NoRouteError:
                break
            details = self._route_details(
                routable_graph,
                path,
                manifest.graph_version,
                origin,
                destination,
                restrictions,
            )
            for edge_id in details.edge_ids:
                edge_usage[edge_id] = edge_usage.get(edge_id, 0) + 1
            if details.summary.route_id in generated_route_ids:
                continue
            generated_route_ids.add(details.summary.route_id)
            if details.summary.travel_time_seconds > fastest.summary.travel_time_seconds * 2:
                continue
            if any(self._edge_overlap(details, prior) >= 0.85 for prior in candidates):
                continue
            candidates.append(details)

        ranked = self._rank_alternatives(candidates, fastest)
        return RouteAlternativesResponse(
            graph_version=manifest.graph_version,
            routes=ranked,
            evaluated_departure_time=departure_time,
            assumptions=[
                "Alternatives use the same active directed restrictions as the comparison.",
                "Routes sharing 85% or more of either candidate's distance are treated as duplicates.",
                "Reliable is an uncalibrated structural proxy favoring fewer penalized local-road segments; it is not a reliability forecast.",
                "Balanced combines free-flow time, residential-road share, and overlap; no live or historical traffic is used.",
            ],
        )

    def google_maps_url(self, request: GoogleMapsUrlRequest) -> GoogleMapsUrlResponse:
        """Create an unkeyed Google Maps URL with a few corridor waypoints."""

        waypoints = self._strategic_waypoints(request.route.geometry.coordinates)
        parameters = {
            "api": "1",
            "origin": f"{request.origin.lat:.6f},{request.origin.lng:.6f}",
            "destination": (
                f"{request.destination.lat:.6f},{request.destination.lng:.6f}"
            ),
            "travelmode": "driving",
        }
        if waypoints:
            parameters["waypoints"] = "|".join(
                f"{latitude:.6f},{longitude:.6f}"
                for longitude, latitude in waypoints
            )
        return GoogleMapsUrlResponse(
            url=f"https://www.google.com/maps/dir/?{urlencode(parameters, safe=',|')}",
            waypoint_count=len(waypoints),
            warning=(
                "Google Maps recalculates independently and may choose a different path. "
                "It does not receive or honor Commute Help road restrictions."
            ),
        )

    def _summarize_route(
        self,
        graph: nx.MultiDiGraph,
        path: list[Any],
        graph_version: str,
        origin: Any,
        destination: Any,
        restrictions: dict[str, AppliedRestriction] | None = None,
    ) -> RouteSummary:
        """Build route geometry and metrics from a directed node path."""

        return self._route_details(
            graph, path, graph_version, origin, destination, restrictions
        ).summary

    def _route_details(
        self,
        graph: nx.MultiDiGraph,
        path: list[Any],
        graph_version: str,
        origin: Any,
        destination: Any,
        restrictions: dict[str, AppliedRestriction] | None = None,
    ) -> RouteDetails:
        coordinates: list[tuple[float, float]] = []
        distance_m = 0.0
        travel_time_seconds = 0.0
        residential_distance_m = 0.0
        structural_penalty_seconds = 0.0
        edge_identity: list[str] = []
        edge_lengths: dict[str, float] = {}
        for u, v in zip(path, path[1:]):
            key, edge = self._best_edge(graph, u, v, restrictions)
            edge_id = str(edge["edge_id"])
            restriction = (restrictions or {}).get(edge_id)
            _, effective_travel_time = self._effective_costs(edge, restriction)
            edge_coordinates = self._oriented_coordinates(graph, u, v, edge)
            if coordinates and edge_coordinates and coordinates[-1] == edge_coordinates[0]:
                coordinates.extend(edge_coordinates[1:])
            else:
                coordinates.extend(edge_coordinates)
            distance_m += float(edge["length_m"])
            travel_time_seconds += effective_travel_time
            edge_length = float(edge["length_m"])
            edge_lengths[edge_id] = edge_lengths.get(edge_id, 0.0) + edge_length
            if str(edge.get("road_class", edge.get("highway", ""))) in {
                "residential",
                "living_street",
            }:
                residential_distance_m += edge_length
            structural_penalty_seconds += max(
                0.0,
                float(edge["routing_cost_seconds"])
                - float(edge["free_flow_seconds"]),
            )
            restriction_identity = (
                f"{restriction.type}:{restriction.remaining_lanes}:"
                f"{restriction.speed_limit_kph}"
                if restriction
                else "baseline"
            )
            edge_identity.append(f"{u}:{v}:{key}:{edge_id}:{restriction_identity}")

        route_hash = hashlib.sha256(
            json.dumps(
                [graph_version, str(origin), str(destination), edge_identity],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return RouteDetails(
            summary=RouteSummary(
                route_id=route_hash,
                travel_time_seconds=round(travel_time_seconds, 1),
                distance_m=round(distance_m, 1),
                geometry=GeoJSONLineString(coordinates=coordinates),
            ),
            edge_ids=frozenset(edge_lengths),
            edge_lengths=edge_lengths,
            residential_distance_m=residential_distance_m,
            structural_penalty_seconds=structural_penalty_seconds,
        )

    def _rank_alternatives(
        self, candidates: list[RouteDetails], fastest: RouteDetails
    ) -> list[AlternativeRoute]:
        remaining = [candidate for candidate in candidates if candidate is not fastest]
        selections: list[tuple[str, RouteDetails, str]] = [
            (
                "fastest",
                fastest,
                "Lowest modeled travel time for the selected departure and restrictions.",
            )
        ]
        if remaining:
            reliable = min(
                remaining,
                key=lambda candidate: (
                    candidate.structural_penalty_seconds
                    / max(candidate.summary.travel_time_seconds, 1),
                    candidate.summary.travel_time_seconds,
                ),
            )
            remaining.remove(reliable)
            selections.append(
                (
                    "reliable",
                    reliable,
                    "Structural proxy with lower exposure to penalized local-road segments; not a reliability forecast.",
                )
            )
        if remaining:
            balanced = min(
                remaining,
                key=lambda candidate: (
                    candidate.summary.travel_time_seconds
                    / max(fastest.summary.travel_time_seconds, 1)
                    + candidate.residential_distance_m
                    / max(candidate.summary.distance_m, 1)
                    + self._edge_overlap(candidate, fastest) * 0.25
                ),
            )
            selections.append(
                (
                    "balanced",
                    balanced,
                    "Balances modeled time, residential-road share, and separation from the fastest corridor.",
                )
            )

        return [
            AlternativeRoute(
                ranking=ranking,
                route=candidate.summary,
                overlap_with_fastest_percent=round(
                    self._edge_overlap(candidate, fastest) * 100, 1
                ),
                residential_distance_percent=round(
                    candidate.residential_distance_m
                    / max(candidate.summary.distance_m, 1)
                    * 100,
                    1,
                ),
                description=description,
            )
            for ranking, candidate, description in selections
        ]

    @staticmethod
    def _edge_overlap(first: RouteDetails, second: RouteDetails) -> float:
        shared = first.edge_ids & second.edge_ids
        shared_distance = sum(
            min(first.edge_lengths[edge_id], second.edge_lengths[edge_id])
            for edge_id in shared
        )
        return shared_distance / max(
            min(first.summary.distance_m, second.summary.distance_m), 1
        )

    @staticmethod
    def _alternative_weight(
        restrictions: dict[str, AppliedRestriction], edge_usage: dict[str, int]
    ) -> WeightFunction:
        def weight(
            _u: Any, _v: Any, candidates: dict[Any, dict[str, Any]]
        ) -> float:
            return min(
                RoutingService._effective_costs(
                    edge, restrictions.get(str(edge["edge_id"]))
                )[0]
                * (1 + 1.4 * edge_usage.get(str(edge["edge_id"]), 0))
                for edge in candidates.values()
            )

        return weight

    @staticmethod
    def _strategic_waypoints(
        coordinates: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        if len(coordinates) <= 2:
            return []
        segment_lengths = [
            _haversine_m(first[1], first[0], second[1], second[0])
            for first, second in zip(coordinates, coordinates[1:])
        ]
        total_distance = sum(segment_lengths)
        if total_distance <= 0:
            return []
        waypoints: list[tuple[float, float]] = []
        cumulative = 0.0
        target_index = 0
        targets = [total_distance * fraction for fraction in (0.25, 0.5, 0.75)]
        for index, segment_length in enumerate(segment_lengths):
            cumulative += segment_length
            while target_index < len(targets) and cumulative >= targets[target_index]:
                candidate = coordinates[index + 1]
                if candidate not in waypoints and candidate not in {
                    coordinates[0],
                    coordinates[-1],
                }:
                    waypoints.append(candidate)
                target_index += 1
        return waypoints[:3]

    def _validate_restrictions(
        self,
        graph: nx.MultiDiGraph,
        request: RouteCompareRequest,
        graph_version: str,
        departure_time: datetime,
    ) -> tuple[dict[str, AppliedRestriction], set[str]]:
        if request.closures and request.graph_version != graph_version:
            raise InvalidClosureError(
                "The closure selection belongs to a different road graph version."
            )

        restrictions: dict[str, AppliedRestriction] = {}
        inactive_edge_ids: set[str] = set()
        seen_edge_ids: set[str] = set()
        for closure in request.closures:
            for edge in closure.edges:
                resolved_u = self.graph_service.node_lookup.get(edge.u)
                resolved_v = self.graph_service.node_lookup.get(edge.v)
                if (
                    resolved_u is None
                    or resolved_v is None
                    or not graph.has_edge(resolved_u, resolved_v, edge.key)
                    or str(graph.edges[resolved_u, resolved_v, edge.key]["edge_id"])
                    != edge.edge_id
                ):
                    raise InvalidClosureError(
                        "A selected closure edge no longer matches the loaded graph."
                    )
                if edge.edge_id in seen_edge_ids:
                    raise InvalidClosureError(
                        "The same directed edge cannot have two restriction definitions."
                    )
                seen_edge_ids.add(edge.edge_id)
                edge_data = graph.edges[resolved_u, resolved_v, edge.key]
                if closure.type == "lane" and closure.remaining_lanes is not None:
                    if closure.remaining_lanes >= float(edge_data.get("lanes", 1)):
                        raise InvalidClosureError(
                            "Remaining lanes must be fewer than the selected road's current lanes."
                        )
                if closure.type == "speed" and closure.speed_limit_kph is not None:
                    if closure.speed_limit_kph >= float(
                        edge_data.get("maxspeed_kph", 35)
                    ):
                        raise InvalidClosureError(
                            "The temporary speed must be below the selected road's current speed."
                        )
                if (
                    closure.starts_at is not None
                    and closure.ends_at is not None
                    and not closure.starts_at <= departure_time < closure.ends_at
                ):
                    inactive_edge_ids.add(edge.edge_id)
                    continue
                restrictions[edge.edge_id] = AppliedRestriction(
                    type=closure.type,
                    remaining_lanes=closure.remaining_lanes,
                    speed_limit_kph=closure.speed_limit_kph,
                )
        return restrictions, inactive_edge_ids

    def find_path(
        self,
        graph: nx.MultiDiGraph,
        origin: Any,
        destination: Any,
        weight: str | WeightFunction = "routing_cost_seconds",
    ) -> list[Any]:
        """Find the directed A* node path, exposed for reference comparisons."""

        try:
            return nx.astar_path(
                graph,
                origin,
                destination,
                heuristic=lambda first, second: self._heuristic(graph, first, second),
                weight=weight,
            )
        except nx.NetworkXNoPath as error:
            raise NoRouteError() from error

    @staticmethod
    def _best_edge(
        graph: nx.MultiDiGraph,
        u: Any,
        v: Any,
        restrictions: dict[str, AppliedRestriction] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        candidates = graph.get_edge_data(u, v)
        if not candidates:
            raise NoRouteError()
        key = min(
            candidates,
            key=lambda candidate_key: RoutingService._effective_costs(
                candidates[candidate_key],
                (restrictions or {}).get(
                    str(candidates[candidate_key]["edge_id"])
                ),
            )[0],
        )
        return int(key), candidates[key]

    @staticmethod
    def _restriction_weight(
        restrictions: dict[str, AppliedRestriction],
    ) -> WeightFunction:
        def weight(
            _u: Any,
            _v: Any,
            candidates: dict[Any, dict[str, Any]],
        ) -> float:
            return min(
                RoutingService._effective_costs(
                    edge,
                    restrictions.get(str(edge["edge_id"])),
                )[0]
                for edge in candidates.values()
            )

        return weight

    @staticmethod
    def _effective_costs(
        edge: dict[str, Any],
        restriction: AppliedRestriction | None,
    ) -> tuple[float, float]:
        free_flow_seconds = float(edge["free_flow_seconds"])
        routing_cost_seconds = float(edge["routing_cost_seconds"])
        if restriction is None or restriction.type == "full":
            return routing_cost_seconds, free_flow_seconds

        if restriction.type == "lane" and restriction.remaining_lanes is not None:
            multiplier = float(edge.get("lanes", 1)) / restriction.remaining_lanes
            return routing_cost_seconds * multiplier, free_flow_seconds * multiplier

        if restriction.type == "speed" and restriction.speed_limit_kph is not None:
            restricted_seconds = float(edge["length_m"]) / (
                restriction.speed_limit_kph / 3.6
            )
            effective_seconds = max(free_flow_seconds, restricted_seconds)
            penalty_ratio = routing_cost_seconds / free_flow_seconds
            return effective_seconds * penalty_ratio, effective_seconds

        raise InvalidClosureError("A road restriction is missing its required value.")

    @staticmethod
    def _oriented_coordinates(
        graph: nx.MultiDiGraph,
        u: Any,
        v: Any,
        edge: dict[str, Any],
    ) -> list[tuple[float, float]]:
        geometry = edge["geometry"]
        coordinates = [(float(x), float(y)) for x, y in geometry.coords]
        u_coordinate = (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"]))
        if _squared_distance(coordinates[-1], u_coordinate) < _squared_distance(
            coordinates[0], u_coordinate
        ):
            coordinates.reverse()
        return coordinates

    @staticmethod
    def _heuristic(graph: nx.MultiDiGraph, first: Any, second: Any) -> float:
        first_node = graph.nodes[first]
        second_node = graph.nodes[second]
        distance_m = _haversine_m(
            float(first_node["y"]),
            float(first_node["x"]),
            float(second_node["y"]),
            float(second_node["x"]),
        )
        return distance_m / MAX_HEURISTIC_SPEED_MPS


def _squared_distance(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lng2 - lng1)
    first_latitude = math.radians(lat1)
    second_latitude = math.radians(lat2)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(haversine))


class NoRouteError(RuntimeError):
    """No directed legal path connects the selected nodes."""


class SameLocationError(ValueError):
    """Both selections resolve to the same routable graph node."""


class InvalidClosureError(ValueError):
    """A closure reference cannot be safely applied to the loaded graph."""
