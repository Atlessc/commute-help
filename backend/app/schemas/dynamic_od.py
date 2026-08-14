"""Typed Phase 3.1 dynamic 15-minute origin-destination contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DYNAMIC_OD_SCHEMA_VERSION = 1
DYNAMIC_OD_PRODUCER_NAME = "historical_dynamic_od_contract_compiler"
DYNAMIC_OD_PRODUCER_VERSION = "phase-3.1-v1"
DYNAMIC_OD_ALGORITHM = "evidence-anchored-dynamic-od-v1"
DYNAMIC_OD_INTERVAL_SECONDS = 900

_SHA256 = r"^[0-9a-f]{64}$"


class Weekday(StrEnum):
    MONDAY = "Monday"
    TUESDAY = "Tuesday"
    WEDNESDAY = "Wednesday"
    THURSDAY = "Thursday"
    FRIDAY = "Friday"


class ZoneType(StrEnum):
    INTERNAL = "internal"
    GATEWAY = "gateway"
    EXTERNAL = "external"
    SPECIAL_GENERATOR = "special_generator"


class MovementClass(StrEnum):
    INTERNAL_INTERNAL = "internal_internal"
    INTERNAL_GATEWAY = "internal_gateway"
    INTERNAL_EXTERNAL = "internal_external"
    INTERNAL_SPECIAL_GENERATOR = "internal_special_generator"
    GATEWAY_INTERNAL = "gateway_internal"
    GATEWAY_GATEWAY = "gateway_gateway"
    GATEWAY_EXTERNAL = "gateway_external"
    GATEWAY_SPECIAL_GENERATOR = "gateway_special_generator"
    EXTERNAL_INTERNAL = "external_internal"
    EXTERNAL_GATEWAY = "external_gateway"
    EXTERNAL_EXTERNAL = "external_external"
    EXTERNAL_SPECIAL_GENERATOR = "external_special_generator"
    SPECIAL_GENERATOR_INTERNAL = "special_generator_internal"
    SPECIAL_GENERATOR_GATEWAY = "special_generator_gateway"
    SPECIAL_GENERATOR_EXTERNAL = "special_generator_external"
    SPECIAL_GENERATOR_SPECIAL_GENERATOR = "special_generator_special_generator"


class EvidenceRole(StrEnum):
    HISTORICAL_FLOW_PROFILE = "historical_flow_profile"
    HISTORICAL_QUALITY_POLICY = "historical_quality_policy"
    HISTORICAL_OBSERVATION_CORPUS = "historical_observation_corpus"
    REGIONAL_OD_SEED = "regional_od_seed"
    ZONE_SYSTEM = "zone_system"
    GATEWAY_INVENTORY = "gateway_inventory"
    VEHICLE_CLASS_SPLIT = "vehicle_class_split"
    BUCKET_SPECIFIC_DETECTOR_EVIDENCE = "bucket_specific_detector_evidence"


class StrictDynamicOdModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DynamicOdEvidenceSource(StrictDynamicOdModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    role: EvidenceRole
    artifact_type: str = Field(min_length=1, max_length=160)
    artifact_id: str = Field(min_length=1, max_length=240)
    content_digest: str = Field(pattern=_SHA256)
    producer_name: str = Field(min_length=1, max_length=160)
    producer_version: str = Field(min_length=1, max_length=160)
    immutable: Literal[True]


class DynamicOdSeedPolicy(StrictDynamicOdModel):
    base_seed: int = Field(ge=0, le=2_147_483_647)
    rng_algorithm: Literal["python-mt19937-v1"]
    seed_derivation: Literal[
        "sha256-base-seed-demand-version-weekday-bucket-vehicle-class-v1"
    ]
    application_status: Literal["declared_for_phase-3.2_not_applied"]


class DynamicOdAssignmentPolicy(StrictDynamicOdModel):
    policy_name: Literal["regional-zone-pair-assignment-v1"]
    path_policy_status: Literal["deferred_to_phase-3.2"]
    selected_trip_independent: Literal[True]


class DynamicOdVehicleSemantics(StrictDynamicOdModel):
    real_vehicles_per_modeled_vehicle: Literal[1.0]
    semantics: Literal["one_sumo_vehicle_per_modeled_vehicle_trip"]
    stochastic_rounding_status: Literal["deferred_to_phase-3.2"]


class DynamicOdFallbackPolicy(StrictDynamicOdModel):
    weekday_substitution: Literal["prohibited"]
    weekday_pooling: Literal["prohibited"]
    season_substitution: Literal["prohibited"]
    exact_date_substitution: Literal["prohibited"]
    missing_bucket_interpolation: Literal["prohibited"]


class DynamicOdTemporalRegularizationPolicy(StrictDynamicOdModel):
    policy_name: Literal["evidence-anchored-adjacent-matrix-v1"]
    adjacent_change_rule: Literal[
        "target_change_requires_bucket_specific_detector_evidence"
    ]
    oscillation_rule: Literal[
        "direction_reversal_requires_bucket_specific_detector_evidence"
    ]
    production_numeric_change_threshold: None = None
    threshold_status: Literal["unresolved_not_invented"]


class DynamicOdRow(StrictDynamicOdModel):
    row_identity: str = Field(pattern=_SHA256)
    weekday: Weekday
    bucket_start_minute: int = Field(ge=0, lt=1_440, multiple_of=15)
    origin_zone_id: str = Field(min_length=1, max_length=160)
    origin_zone_type: ZoneType
    destination_zone_id: str = Field(min_length=1, max_length=160)
    destination_zone_type: ZoneType
    movement_class: MovementClass
    vehicle_class: str = Field(min_length=1, max_length=120)
    target_vehicle_trips: float = Field(ge=0)
    target_flow_vph: float = Field(ge=0)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    adjacent_change_evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_movement_and_units(self) -> DynamicOdRow:
        expected = f"{self.origin_zone_type.value}_{self.destination_zone_type.value}"
        if self.movement_class.value != expected:
            raise ValueError("movement_class must match origin/destination zone types")
        if abs(self.target_flow_vph - self.target_vehicle_trips * 4.0) > 1e-9:
            raise ValueError("target_flow_vph must equal 15-minute trips multiplied by 4")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("row evidence_ids must be unique")
        if len(self.adjacent_change_evidence_ids) != len(
            set(self.adjacent_change_evidence_ids)
        ):
            raise ValueError("adjacent change evidence IDs must be unique")
        return self


class DynamicOdMovementTotal(StrictDynamicOdModel):
    movement_class: MovementClass
    target_vehicle_trips: float = Field(ge=0)
    target_flow_vph: float = Field(ge=0)


class DynamicOdBucketConservation(StrictDynamicOdModel):
    weekday: Weekday
    bucket_start_minute: int = Field(ge=0, lt=1_440, multiple_of=15)
    row_count: int = Field(ge=1)
    target_vehicle_trips: float = Field(ge=0)
    target_flow_vph: float = Field(ge=0)
    movement_totals: tuple[DynamicOdMovementTotal, ...] = Field(min_length=1)


class DynamicOdArtifactV1(StrictDynamicOdModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_dynamic_od_time_series"]
    artifact_status: Literal["candidate_unvalidated"]
    producer_name: Literal["historical_dynamic_od_contract_compiler"]
    producer_version: Literal["phase-3.1-v1"]
    algorithm: Literal["evidence-anchored-dynamic-od-v1"]
    demand_version: str = Field(min_length=1, max_length=160)
    generated_at: datetime
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    bucket_end_semantics: Literal["half_open_start_inclusive_end_exclusive"]
    evidence_level: Literal["historical_evidence_derived_candidate"]
    calibration_status: Literal["not_calibrated"]
    weekdays: tuple[Weekday, ...] = Field(min_length=1)
    bucket_start_minutes: tuple[int, ...] = Field(min_length=1)
    evidence_sources: tuple[DynamicOdEvidenceSource, ...] = Field(min_length=1)
    seed_policy: DynamicOdSeedPolicy
    assignment_policy: DynamicOdAssignmentPolicy
    vehicle_semantics: DynamicOdVehicleSemantics
    fallback_policy: DynamicOdFallbackPolicy
    temporal_regularization: DynamicOdTemporalRegularizationPolicy
    rows: tuple[DynamicOdRow, ...] = Field(min_length=1)
    conservation: tuple[DynamicOdBucketConservation, ...] = Field(min_length=1)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_header(self) -> DynamicOdArtifactV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("weekdays must be unique")
        if len(self.bucket_start_minutes) != len(set(self.bucket_start_minutes)):
            raise ValueError("bucket_start_minutes must be unique")
        if any(value % 15 or value < 0 or value >= 1_440 for value in self.bucket_start_minutes):
            raise ValueError("bucket starts must be aligned 15-minute local-day minutes")
        return self
