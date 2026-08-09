"""Private-safe local benchmark trip contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkTripInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    departure_time: datetime
    day_type: Literal["mon_thu", "friday", "saturday", "sunday"]
    origin_node: str | None = None
    destination_node: str | None = None
    origin_zone: str | None = None
    destination_zone: str | None = None
    corridor_label: str = Field(min_length=1, max_length=120)
    actual_travel_seconds: float = Field(gt=0, le=86_400)
    reported_no_traffic_seconds: float | None = Field(default=None, gt=0, le=86_400)
    incident_flag: bool = False
    weather_category: str | None = Field(default=None, max_length=80)
    checkpoints: list[dict] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_endpoints(self) -> "BenchmarkTripInput":
        if self.departure_time.tzinfo is None:
            raise ValueError("Benchmark departure_time must include a timezone")
        node_pair = self.origin_node is not None and self.destination_node is not None
        zone_pair = self.origin_zone is not None and self.destination_zone is not None
        if node_pair == zone_pair:
            raise ValueError("Provide exactly one complete node pair or zone pair")
        return self


class BenchmarkTripRecord(BenchmarkTripInput):
    graph_version: str
    created_at: datetime
    updated_at: datetime


class FreeFlowValidation(BaseModel):
    benchmark_id: str
    graph_version: str
    network_free_flow_seconds: float = Field(gt=0)
    tolerance_seconds: float = Field(ge=0)
    minimum_allowed_seconds: float = Field(gt=0)
    observed_or_simulated_seconds: float = Field(gt=0)
    value_kind: Literal["observed", "reported_no_traffic", "simulated"]
    valid: bool
    violation_seconds: float = Field(ge=0)
    path_edge_count: int = Field(gt=0)
    path_distance_m: float = Field(gt=0)
    evidence_level: Literal["network_physical_floor"] = "network_physical_floor"


class PlanningTimeResult(BaseModel):
    planning_mode: Literal["depart_at", "arrive_by"]
    departure_time: datetime
    arrival_time: datetime
    travel_time_seconds: float = Field(gt=0)
    search_iterations: int = Field(ge=0)
