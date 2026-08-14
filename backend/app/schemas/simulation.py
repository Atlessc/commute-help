"""Typed contracts for the local physical-simulation subsystem."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class SimulationCapabilities(BaseModel):
    """Safe, path-free readiness information for the local SUMO runtime."""

    available: bool
    sumo_version: str | None = None
    runtime_mode: Literal["libsumo", "subprocess"] | None = None
    sumo_available: bool
    netconvert_available: bool
    sumolib_available: bool
    libsumo_available: bool
    network_ready: bool
    schedule_ready: bool = False
    schedule_version: str | None = None
    proxy_demand_ready: bool = False
    proxy_demand_model_version: str | None = None
    model_ready: bool
    active_run_id: str | None = None
    max_parallel_runs: int = Field(ge=1)
    offline_only: bool
    warnings: list[str] = Field(default_factory=list)


class RegionalClosureInput(BaseModel):
    """One generic, directed app closure translated by the worker."""

    app_edge_ids: list[str] = Field(min_length=1)
    restriction_type: Literal["full", "lane", "speed"]
    remaining_lanes: int | None = Field(default=None, ge=1)
    speed_limit_kph: float | None = Field(default=None, gt=0, le=160)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def validate_restriction(self) -> "RegionalClosureInput":
        if self.restriction_type == "lane" and self.remaining_lanes is None:
            raise ValueError("remaining_lanes is required for a lane restriction")
        if self.restriction_type == "speed" and self.speed_limit_kph is None:
            raise ValueError("speed_limit_kph is required for a speed restriction")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("closure schedule requires both starts_at and ends_at")
        if self.starts_at is not None:
            if self.starts_at.tzinfo is None or self.ends_at is None or self.ends_at.tzinfo is None:
                raise ValueError("closure schedule timestamps must include a timezone")
            if self.ends_at <= self.starts_at:
                raise ValueError("closure schedule must end after it starts")
        return self


class SimulationRunRequest(BaseModel):
    """A toy validation run or a physical regional baseline/closure comparison."""

    run_kind: Literal["validation", "regional_comparison"] = "validation"
    fixture: Literal["tiny_closure"] = "tiny_closure"
    seed: int = Field(default=842901, ge=0, le=2_147_483_647)
    step_delay_ms: int = Field(default=0, ge=0, le=250, exclude=True)
    departure_time: datetime | None = None
    origin_app_edge_id: str | None = None
    destination_app_edge_id: str | None = None
    origin_node_id: str | None = None
    destination_node_id: str | None = None
    closures: list[RegionalClosureInput] = Field(default_factory=list, max_length=200)
    warmup_minutes: int = Field(default=45, ge=0, le=180)
    analysis_minutes: int = Field(default=60, ge=5, le=240)
    real_vehicles_per_simulated_vehicle: float = Field(default=1.0, ge=1, le=100)
    frame_interval_seconds: int = Field(default=15, ge=5, le=60)
    aggregate_interval_seconds: int = Field(default=60, ge=30, le=300)
    reroute_period_seconds: int = Field(default=60, ge=15, le=300)
    edge_telemetry_interval_seconds: Literal[900] | None = None
    station_telemetry_interval_seconds: Literal[900] | None = None
    station_cross_section_policy_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_run_kind(self) -> "SimulationRunRequest":
        station_enabled = self.station_telemetry_interval_seconds is not None
        if station_enabled != (self.station_cross_section_policy_digest is not None):
            raise ValueError(
                "Station telemetry requires both the 900-second interval and policy digest"
            )
        if self.run_kind == "regional_comparison":
            if self.departure_time is None or self.departure_time.tzinfo is None:
                raise ValueError("regional departure_time must include a timezone")
            if not self.origin_app_edge_id or not self.destination_app_edge_id:
                raise ValueError("regional runs require origin and destination app edges")
            if not self.origin_node_id or not self.destination_node_id:
                raise ValueError("regional runs require origin and destination graph nodes")
            if not self.closures:
                raise ValueError("regional comparison requires at least one closure")
        return self


class SimulationRunCreated(BaseModel):
    id: UUID
    status: Literal["queued", "running"]


class SimulationRunStatus(BaseModel):
    id: UUID
    run_kind: str
    status: Literal[
        "queued", "running", "completed", "failed", "cancel_requested", "cancelled"
    ]
    seed: int
    progress: float = Field(ge=0, le=1)
    current_sim_second: float | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict | None = None
    error_message: str | None = None


class SimulationPlayback(BaseModel):
    """Bounded playback payload produced by a completed physical run."""

    schema_version: int
    duration_seconds: float = Field(ge=0)
    frame_interval_seconds: int = Field(ge=1)
    seed: int
    real_vehicles_per_simulated_vehicle: float = Field(ge=1)
    displayed_vehicle_limit: int = Field(ge=1)
    frames: list[dict]
