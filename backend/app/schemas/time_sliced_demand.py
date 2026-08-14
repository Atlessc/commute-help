"""Typed Phase 3.2 bucket-contained SUMO demand artifact contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIME_SLICED_DEMAND_SCHEMA_VERSION = 1
TIME_SLICED_DEMAND_PRODUCER_NAME = "dynamic_od_sumo_demand_generator"
TIME_SLICED_DEMAND_PRODUCER_VERSION = "phase-3.2-v1"
TIME_SLICED_DEMAND_ALGORITHM = "bucket-contained-balanced-rounding-v1"
TIME_SLICED_DEMAND_FILE = "time-sliced-demand.trips.xml.gz"

_SHA256 = r"^[0-9a-f]{64}$"


class StrictTimeSlicedDemandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeSlicedSeedLineage(StrictTimeSlicedDemandModel):
    source_base_seed: int = Field(ge=0, le=2_147_483_647)
    effective_seed: int = Field(ge=0, le=2_147_483_647)
    seed_override_applied: bool
    source_rng_algorithm: str = Field(min_length=1, max_length=120)
    source_seed_derivation: str = Field(min_length=1, max_length=200)
    generation_seed_derivation: Literal[
        "sha256-effective-seed-demand-version-weekday-bucket-scope-v1"
    ]


class TimeSlicedRowRoundingAudit(StrictTimeSlicedDemandModel):
    source_row_identity: str = Field(pattern=_SHA256)
    origin_zone_id: str = Field(min_length=1, max_length=160)
    destination_zone_id: str = Field(min_length=1, max_length=160)
    movement_class: str = Field(min_length=1, max_length=160)
    vehicle_class: str = Field(min_length=1, max_length=120)
    target_vehicle_trips: float = Field(ge=0)
    generated_vehicle_count: int = Field(ge=0)
    represented_vehicle_trips: float = Field(ge=0)
    rounding_error_vehicle_trips: float
    allowed_absolute_error_vehicle_trips: Literal[1.0]
    error_within_bound: bool


class TimeSlicedClassConservation(StrictTimeSlicedDemandModel):
    class_name: str = Field(min_length=1, max_length=160)
    target_vehicle_trips: float = Field(ge=0)
    represented_vehicle_trips: float = Field(ge=0)
    rounding_error_vehicle_trips: float


class TimeSlicedBucketAudit(StrictTimeSlicedDemandModel):
    weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    bucket_start_minute: int = Field(ge=0, lt=1_440, multiple_of=15)
    interval_start_seconds: int = Field(ge=0)
    interval_end_seconds: int = Field(gt=0)
    source_row_count: int = Field(ge=1)
    target_vehicle_trips: float = Field(ge=0)
    generated_vehicle_count: int = Field(ge=0)
    represented_vehicle_trips: float = Field(ge=0)
    rounding_error_vehicle_trips: float
    allowed_absolute_aggregate_error_vehicle_trips: Literal[1.0]
    maximum_absolute_row_error_vehicle_trips: float = Field(ge=0)
    rounding_gate_passed: bool
    departure_leakage_count: int = Field(ge=0)
    minimum_departure_seconds: float | None = Field(default=None, ge=0)
    maximum_departure_seconds: float | None = Field(default=None, ge=0)
    movement_class_conservation: tuple[TimeSlicedClassConservation, ...]
    vehicle_class_conservation: tuple[TimeSlicedClassConservation, ...]
    rows: tuple[TimeSlicedRowRoundingAudit, ...]

    @model_validator(mode="after")
    def validate_interval(self) -> TimeSlicedBucketAudit:
        if self.interval_end_seconds - self.interval_start_seconds != 900:
            raise ValueError("time-sliced demand bucket must be exactly 900 seconds")
        return self


class TimeSlicedDepartureAudit(StrictTimeSlicedDemandModel):
    interval_seconds: Literal[900]
    generated_vehicle_count: int = Field(ge=0)
    leakage_count: int = Field(ge=0)
    departure_at_interval_end_count: int = Field(ge=0)
    duplicate_vehicle_identity_count: int = Field(ge=0)
    gate_passed: bool


class TimeSlicedRoundingAudit(StrictTimeSlicedDemandModel):
    policy: Literal["balanced-stochastic-rounding-per-weekday-bucket-v1"]
    scale_real_vehicles_per_sumo_vehicle: Literal[1.0]
    per_row_absolute_error_bound_vehicle_trips: Literal[1.0]
    per_bucket_absolute_error_bound_vehicle_trips: Literal[1.0]
    target_vehicle_trips: float = Field(ge=0)
    represented_vehicle_trips: float = Field(ge=0)
    aggregate_rounding_error_vehicle_trips: float
    maximum_absolute_row_error_vehicle_trips: float = Field(ge=0)
    gate_passed: bool


class TimeSlicedDemandOutputIdentity(StrictTimeSlicedDemandModel):
    relative_path: Literal["time-sliced-demand.trips.xml.gz"]
    sha256: str = Field(pattern=_SHA256)
    uncompressed_sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)


class TimeSlicedDemandManifestV1(StrictTimeSlicedDemandModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_time_sliced_sumo_demand"]
    artifact_status: Literal["candidate_unvalidated"]
    producer_name: Literal["dynamic_od_sumo_demand_generator"]
    producer_version: Literal["phase-3.2-v1"]
    algorithm: Literal["bucket-contained-balanced-rounding-v1"]
    generated_at: datetime
    source_dynamic_od_content_digest: str = Field(pattern=_SHA256)
    source_demand_version: str = Field(min_length=1, max_length=160)
    source_evidence_content_digests: tuple[str, ...] = Field(min_length=1)
    demand_version: str = Field(min_length=1, max_length=200)
    weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    bucket_end_semantics: Literal["half_open_start_inclusive_end_exclusive"]
    bucket_start_minutes: tuple[int, ...] = Field(min_length=1)
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]
    routing_status: Literal["unrouted_zone_pair_demand"]
    assignment_policy_name: str = Field(min_length=1, max_length=160)
    seed_lineage: TimeSlicedSeedLineage
    real_vehicles_per_modeled_vehicle: Literal[1.0]
    generated_vehicle_count: int = Field(ge=0)
    represented_target_vehicle_trips: float = Field(ge=0)
    bucket_audits: tuple[TimeSlicedBucketAudit, ...] = Field(min_length=1)
    rounding_audit: TimeSlicedRoundingAudit
    departure_audit: TimeSlicedDepartureAudit
    output: TimeSlicedDemandOutputIdentity
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> TimeSlicedDemandManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.bucket_start_minutes != tuple(sorted(set(self.bucket_start_minutes))):
            raise ValueError("bucket_start_minutes must be unique and ordered")
        return self
