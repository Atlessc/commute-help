"""Traffic import, profile, and reliability API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.routing import RouteSummary


class TrafficDataQuality(BaseModel):
    row_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    matched_station_count: int = Field(ge=0)
    unmatched_station_count: int = Field(ge=0)
    missing_speed_percent: float = Field(ge=0, le=100)
    missing_volume_percent: float = Field(ge=0, le=100)
    quality_flags: list[str]


class TrafficProfile(BaseModel):
    id: UUID
    name: str
    version: str
    source_name: str
    source_window: str
    period: Literal["weekday_morning", "weekday_afternoon"]
    graph_version: str
    observation_count: int = Field(ge=1)
    volume_observation_count: int = Field(default=0, ge=0)
    matched_edge_count: int = Field(default=0, ge=0)
    median_multiplier: float = Field(ge=1)
    p85_multiplier: float = Field(ge=1)
    p90_multiplier: float = Field(ge=1)
    p95_multiplier: float = Field(ge=1)
    created_at: datetime


class TrafficProfilesResponse(BaseModel):
    profiles: list[TrafficProfile]


class TrafficImportResponse(BaseModel):
    import_id: UUID
    source_name: str
    raw_sha256: str
    normalized_path: str
    quality: TrafficDataQuality
    profiles: list[TrafficProfile]


class ReliabilityRequest(BaseModel):
    route: RouteSummary
    planning_mode: Literal["arrive_by", "depart_at"] = "arrive_by"
    departure_time: datetime
    arrival_deadline: datetime | None = None
    buffer_minutes: int = Field(default=8, ge=0, le=180)
    confidence_target: float = Field(default=0.9, ge=0.5, le=0.99)
    sample_count: int = Field(default=1000, ge=100, le=10000)
    profile_id: UUID | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "ReliabilityRequest":
        if not _aware(self.departure_time):
            raise ValueError("Departure time must include a timezone.")
        if self.planning_mode == "arrive_by":
            if self.arrival_deadline is None or not _aware(self.arrival_deadline):
                raise ValueError("Arrive-by planning requires a timezone-aware deadline.")
            if self.arrival_deadline <= self.departure_time:
                raise ValueError("Arrival deadline must be after departure time.")
        return self


class EarlyDepartureBenefit(BaseModel):
    minutes_earlier: Literal[5, 10, 15]
    on_time_probability: float = Field(ge=0, le=1)
    improvement: float = Field(ge=0, le=1)


class ReliabilityResponse(BaseModel):
    planning_mode: Literal["arrive_by", "depart_at"]
    evidence_level: Literal["modeled_uncalibrated", "historically_calibrated"]
    mean_seconds: float = Field(ge=0)
    median_seconds: float = Field(ge=0)
    p85_seconds: float = Field(ge=0)
    p90_seconds: float = Field(ge=0)
    p95_seconds: float = Field(ge=0)
    likely_low_seconds: float = Field(ge=0)
    likely_high_seconds: float = Field(ge=0)
    on_time_probability: float | None = Field(default=None, ge=0, le=1)
    latest_safe_departure: datetime | None = None
    planned_departure_time: datetime
    median_arrival_time: datetime
    p85_arrival_time: datetime
    p90_arrival_time: datetime
    p95_arrival_time: datetime
    confidence_arrival_time: datetime
    confidence_target: float
    sample_count: int
    source_name: str
    source_window: str
    profile_version: str
    early_departure_benefits: list[EarlyDepartureBenefit]
    assumptions: list[str]


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
