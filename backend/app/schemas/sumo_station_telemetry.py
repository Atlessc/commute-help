"""Typed Phase 2.2e regional station-crossing telemetry contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STATION_TELEMETRY_SCHEMA_VERSION = 1
STATION_TELEMETRY_PRODUCER_VERSION = "phase-2.2e-v1"
STATION_OBSERVATION_PLAN_VERSION = "phase-2.2e-plan-v1"
STATION_OBSERVER_STATE_VERSION = 2
STATION_OBSERVER_ALGORITHM = "ordered-position-transition-v1"
STATION_OBSERVER_IMPLEMENTATION = "subscription-filtered-v1"
STATION_TELEMETRY_INTERVAL_SECONDS = 900

_SHA256 = r"^[0-9a-f]{64}$"


class StrictStationTelemetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StationTelemetryFileIdentity(StrictStationTelemetryModel):
    relative_path: str
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int = Field(ge=0)


class StationTelemetryRecordV1(StrictStationTelemetryModel):
    schema_version: Literal[1]
    run_identity: str
    network_version: str
    sumo_version: str
    simulation_mode: Literal["mesoscopic"]
    seed: int = Field(ge=0)
    demand_version: str
    variant: Literal["baseline", "scenario"]
    station_id: str
    app_edge_id: str
    cross_section_type: Literal["within_edge_position", "edge_transition"]
    sumo_edge_id: str | None
    transition_from_sumo_edge_id: str | None
    transition_to_sumo_edge_id: str | None
    interval_start_seconds: int = Field(ge=0)
    interval_end_seconds: int = Field(gt=0)
    interval_duration_seconds: Literal[900]
    crossing_count: int = Field(ge=0)
    flow_vph: float = Field(ge=0)
    contributing_vehicle_count: int = Field(ge=0)
    resolved_direct_departure_count: int = Field(default=0, ge=0)
    unresolved_direct_departure_count: int = Field(default=0, ge=0)
    flow_measurement_complete: bool = True
    crossing_step_speed_sample_count: Literal[0]
    crossing_step_speed_mean_mps: None = None
    crossing_step_speed_median_mps: None = None
    policy_name: Literal["historical_station_sumo_cross_section_policy"]
    policy_version: Literal["phase-2.2d-policy-v1"]
    policy_content_digest: str = Field(pattern=_SHA256)
    observation_plan_digest: str = Field(pattern=_SHA256)
    observer_algorithm: Literal["ordered-position-transition-v1"]
    observer_implementation: Literal["subscription-filtered-v1"]
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]

    @model_validator(mode="after")
    def validate_semantics(self) -> StationTelemetryRecordV1:
        if self.interval_end_seconds - self.interval_start_seconds != 900:
            raise ValueError("Station telemetry interval must be exactly 900 seconds")
        if abs(self.flow_vph - self.crossing_count * 4.0) > 1e-9:
            raise ValueError("Station flow_vph must equal crossing_count times four")
        if self.contributing_vehicle_count > self.crossing_count:
            raise ValueError("Contributing vehicles cannot exceed crossings")
        if self.flow_measurement_complete != (
            self.unresolved_direct_departure_count == 0
        ):
            raise ValueError(
                "Flow completeness must equal zero unresolved direct departures"
            )
        if self.cross_section_type == "within_edge_position":
            if not self.sumo_edge_id or self.transition_from_sumo_edge_id is not None:
                raise ValueError("Within-edge telemetry requires only sumo_edge_id")
        elif (
            not self.transition_from_sumo_edge_id or not self.transition_to_sumo_edge_id
        ):
            raise ValueError("Transition telemetry requires both transition members")
        return self


class StationTelemetryManifestV1(StrictStationTelemetryModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_sumo_station_crossing_telemetry"]
    artifact_status: Literal["complete"]
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["regional_sumo_station_crossing_telemetry"]
    producer_version: Literal["phase-2.2e-v1"]
    observer_algorithm: Literal["ordered-position-transition-v1"]
    observer_implementation: Literal["subscription-filtered-v1"]
    application_run_id: str
    run_identity: str
    graph_version: str
    network_version: str
    sumo_version: str
    simulation_mode: Literal["mesoscopic"]
    seed: int = Field(ge=0)
    demand_version: str
    demand_routes_sha256: str = Field(pattern=_SHA256)
    policy_name: Literal["historical_station_sumo_cross_section_policy"]
    policy_version: Literal["phase-2.2d-policy-v1"]
    policy_content_digest: str = Field(pattern=_SHA256)
    observation_plan_version: Literal["phase-2.2e-plan-v1"]
    observation_plan_digest: str = Field(pattern=_SHA256)
    station_count: Literal[356]
    interval_seconds: Literal[900]
    interval_semantics: Literal["half_open_start_inclusive_end_exclusive"]
    simulation_start_seconds: int = Field(ge=0)
    simulation_end_seconds: int = Field(gt=0)
    complete_interval_count_per_variant: int = Field(ge=0)
    omitted_partial_seconds_at_end: int = Field(ge=0, lt=900)
    variants: tuple[Literal["baseline"], Literal["scenario"]]
    direct_departure_policy: Literal[
        "numeric_authoritative_depart_position_or_conservative_no_crossing",
        "native_departure_child_or_station_local_incomplete",
    ]
    departure_provenance_version: str | None = None
    speed_semantics: Literal["not_collected_point_speed_ineligible"]
    output: StationTelemetryFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> StationTelemetryManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        expected = self.station_count * self.complete_interval_count_per_variant * 2
        if self.output.row_count != expected:
            raise ValueError(
                "Station telemetry must be dense by station/variant/interval"
            )
        return self


class StationTelemetryRecoveryFileV1(StrictStationTelemetryModel):
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)


class StationTelemetryRecoveryCheckpointV1(StrictStationTelemetryModel):
    variant: Literal["baseline", "scenario"]
    chunk_index: int = Field(ge=1)
    chunk_start_second: int = Field(ge=0)
    chunk_end_second: int = Field(gt=0)
    station_fragment_row_count: int = Field(ge=0)
    departure_provenance_content_digest: str = Field(pattern=_SHA256)
    files: dict[str, StationTelemetryRecoveryFileV1]


class StationTelemetryRecoveryManifestV1(StrictStationTelemetryModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_station_telemetry_checkpoint_recovery"]
    artifact_status: Literal["complete"]
    generated_at: datetime
    producer_name: Literal["station_telemetry_checkpoint_recovery"]
    producer_version: Literal["phase-2.2g-recovery-v1"]
    validation_algorithm: Literal["promoted-checkpoint-chain-v1"]
    application_run_id: str
    run_identity: str
    source_run_wrapper_status: Literal["failed"]
    source_run_wrapper_error: str
    recovery_reason: Literal["post_checkpoint_selected_trip_diagnostic_failure"]
    checkpoint_count: Literal[18]
    final_checkpoint_second: Literal[900]
    network_version: str
    graph_version: str
    seed: int = Field(ge=0)
    demand_version: str
    demand_routes_sha256: str = Field(pattern=_SHA256)
    policy_content_digest: str = Field(pattern=_SHA256)
    observation_plan_digest: str = Field(pattern=_SHA256)
    checkpoints: tuple[StationTelemetryRecoveryCheckpointV1, ...]
    selected_trip_diagnostic: dict[str, object]
    station_telemetry_content_digest: str | None = Field(default=None, pattern=_SHA256)
    station_telemetry_output_sha256: str | None = Field(default=None, pattern=_SHA256)
    station_telemetry_row_count: int | None = Field(default=None, ge=0)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_recovery_manifest(self) -> StationTelemetryRecoveryManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if len(self.checkpoints) != self.checkpoint_count:
            raise ValueError(
                "checkpoint recovery inventory must contain 18 checkpoints"
            )
        return self
