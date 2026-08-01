"""Typed map selection, closure, and routing API schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Coordinate(BaseModel):
    """A WGS84 coordinate supplied or returned through the API."""

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RoadSelectionRequest(Coordinate):
    """Map click to snap against the local routable network."""


class DirectedEdgeReference(BaseModel):
    """Stable identity for the closest directed road edge."""

    edge_id: str
    u: str
    v: str
    key: int
    road_name: str
    road_class: str
    osm_way_ids: list[str] = Field(default_factory=list)
    geometry_fingerprint: str = ""
    lanes: float = Field(gt=0)
    maxspeed_kph: float = Field(gt=0)


class SnappedLocation(Coordinate):
    """A map point resolved to a routable graph node and nearby road."""

    node_id: str
    distance_m: float = Field(ge=0)
    label: str
    edge: DirectedEdgeReference


class RoadSelectionResponse(BaseModel):
    """Confirmed local-network selection for the frontend workflow."""

    graph_version: str
    location: SnappedLocation


class ClosureDirectionCandidate(BaseModel):
    """One directed edge a user can explicitly close."""

    edge: DirectedEdgeReference
    direction_label: str
    geometry: "GeoJSONLineString"


class ClosureRoadSelectionResponse(BaseModel):
    """The nearest road and each independently closable direction."""

    graph_version: str
    road_name: str
    distance_m: float = Field(ge=0)
    selected_edge_id: str
    directions: list[ClosureDirectionCandidate] = Field(min_length=1)


class RouteLocation(Coordinate):
    """A trip endpoint, optionally retaining a previously snapped node."""

    node_id: str | None = None


class ClosureEdgeInput(BaseModel):
    """Graph-version-specific directed edge selected by the user."""

    edge_id: str
    u: str
    v: str
    key: int


class ClosureInput(BaseModel):
    """One restriction applied to explicitly selected directed edges."""

    type: Literal["full", "lane", "speed"] = "full"
    edges: list[ClosureEdgeInput] = Field(min_length=1)
    remaining_lanes: float | None = Field(default=None, gt=0)
    speed_limit_kph: float | None = Field(default=None, ge=5, le=130)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def validate_restriction_value(self) -> "ClosureInput":
        """Require only the value used by the selected restriction type."""

        if self.type == "lane" and self.remaining_lanes is None:
            raise ValueError("Lane restrictions require remaining_lanes.")
        if self.type == "speed" and self.speed_limit_kph is None:
            raise ValueError("Speed restrictions require speed_limit_kph.")
        if self.type != "lane" and self.remaining_lanes is not None:
            raise ValueError("remaining_lanes is only valid for lane restrictions.")
        if self.type != "speed" and self.speed_limit_kph is not None:
            raise ValueError("speed_limit_kph is only valid for speed restrictions.")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("Scheduled restrictions require both starts_at and ends_at.")
        if self.starts_at is not None and self.ends_at is not None:
            if not _is_timezone_aware(self.starts_at) or not _is_timezone_aware(
                self.ends_at
            ):
                raise ValueError("Restriction schedules must include a timezone.")
            if self.ends_at <= self.starts_at:
                raise ValueError("Restriction end time must be after its start time.")
        return self


class RouteCompareRequest(BaseModel):
    """Trip request with optional directed road restrictions."""

    origin: RouteLocation
    destination: RouteLocation
    graph_version: str | None = None
    departure_time: datetime | None = None
    closures: list[ClosureInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_departure_time(self) -> "RouteCompareRequest":
        if self.departure_time is not None and not _is_timezone_aware(
            self.departure_time
        ):
            raise ValueError("departure_time must include a timezone.")
        return self


class GeoJSONLineString(BaseModel):
    """Small route geometry for MapLibre display."""

    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_coordinates(self) -> "GeoJSONLineString":
        for longitude, latitude in self.coordinates:
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise ValueError("Route geometry contains an invalid coordinate.")
        return self


class RouteSummary(BaseModel):
    """A free-flow route with honest evidence and display metrics."""

    route_id: str
    travel_time_seconds: float = Field(ge=0)
    distance_m: float = Field(ge=0)
    geometry: GeoJSONLineString


class RouteCompareResponse(BaseModel):
    """Baseline and closure-aware results from the same immutable graph."""

    graph_version: str
    evidence_level: Literal["free_flow", "modeled_uncalibrated"]
    baseline: RouteSummary
    scenario: RouteSummary | None = None
    scenario_status: Literal["not_requested", "available", "no_route", "inactive"] = (
        "not_requested"
    )
    evaluated_departure_time: datetime
    applied_restriction_edge_ids: list[str] = Field(default_factory=list)
    inactive_restriction_edge_ids: list[str] = Field(default_factory=list)
    assumptions: list[str]


class AlternativeRoute(BaseModel):
    """One distinct corridor with transparent ranking inputs."""

    ranking: Literal["fastest", "reliable", "balanced"]
    route: RouteSummary
    overlap_with_fastest_percent: float = Field(ge=0, le=100)
    residential_distance_percent: float = Field(ge=0, le=100)
    description: str


class RouteAlternativesResponse(BaseModel):
    """Meaningfully distinct closure-aware route choices."""

    graph_version: str
    evidence_level: Literal["modeled_uncalibrated"] = "modeled_uncalibrated"
    routes: list[AlternativeRoute] = Field(min_length=1, max_length=3)
    evaluated_departure_time: datetime
    assumptions: list[str]


class GoogleMapsUrlRequest(BaseModel):
    """A backend-produced route and its trip endpoints for handoff."""

    origin: Coordinate
    destination: Coordinate
    route: RouteSummary


class GoogleMapsUrlResponse(BaseModel):
    """Standard unkeyed Google Maps directions URL and disclosure."""

    url: str
    waypoint_count: int = Field(ge=0, le=3)
    warning: str


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
