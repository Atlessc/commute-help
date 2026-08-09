"""Typed network-diversion job and result contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.routing import ClosureInput, GeoJSONLineString, RouteLocation
from backend.app.schemas.routing import RouteSummary


class DiversionRequest(BaseModel):
    """A bounded synthetic demand experiment around a closure scenario."""

    origin: RouteLocation
    destination: RouteLocation
    graph_version: str
    departure_time: datetime
    closures: list[ClosureInput] = Field(min_length=1)
    demand_vph: int = Field(default=600, ge=50, le=5000)
    demand_pair_count: int = Field(default=20, ge=5, le=100)
    iterations: int = Field(default=4, ge=2, le=10)
    dispersion_radius_m: int = Field(default=1500, ge=0, le=10000)
    max_result_edges: int = Field(default=200, ge=25, le=500)
    traffic_profile_id: UUID | None = None

    @model_validator(mode="after")
    def validate_departure(self) -> "DiversionRequest":
        if (
            self.departure_time.tzinfo is None
            or self.departure_time.utcoffset() is None
        ):
            raise ValueError("departure_time must include a timezone.")
        return self


class DiversionEdgeChange(BaseModel):
    edge_id: str
    u: str
    v: str
    key: int
    road_name: str
    road_class: str
    baseline_vph: float = Field(ge=0)
    scenario_vph: float = Field(ge=0)
    change_vph: float
    change_percent: float | None = None
    volume_capacity_ratio: float = Field(ge=0)
    geometry: GeoJSONLineString


class DiversionPlaybackEdge(BaseModel):
    """Bounded road state used to render traffic samples during playback."""

    edge_id: str
    baseline_vph: float = Field(ge=0)
    scenario_vph: float = Field(ge=0)
    volume_capacity_ratio: float = Field(ge=0)
    geometry: GeoJSONLineString


class DiversionResult(BaseModel):
    evidence_level: Literal["modeled_uncalibrated"] = "modeled_uncalibrated"
    graph_version: str
    model_version: str
    input_hash: str
    demand_vph: int
    demand_pair_count: int
    iterations: int
    dispersion_radius_m: int
    traffic_profile_id: UUID | None = None
    background_source: str
    background_bucket: str
    background_observation_count: int = Field(ge=0)
    background_matched_edge_count: int = Field(ge=0)
    background_network_coverage_percent: float = Field(ge=0, le=100)
    displaced_background_vph: float = Field(ge=0)
    assigned_demand_vph: float = Field(ge=0)
    unassigned_demand_vph: float = Field(ge=0)
    changed_edge_count: int = Field(ge=0)
    max_increase_vph: float = Field(ge=0)
    max_decrease_vph: float = Field(le=0)
    residential_increase_vph: float = Field(ge=0)
    recommended_route: RouteSummary | None = None
    edge_changes: list[DiversionEdgeChange]
    playback_edges: list[DiversionPlaybackEdge] = Field(default_factory=list)
    assumptions: list[str]


class DiversionJobResponse(BaseModel):
    id: UUID
    status: Literal["queued", "running", "completed", "cancelled", "failed"]
    progress_percent: int = Field(ge=0, le=100)
    message: str
    cached: bool = False
    created_at: datetime
    updated_at: datetime
    result: DiversionResult | None = None
    error: str | None = None
