"""Typed contracts for native SUMO 15-minute edge telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUMO_EDGE_TELEMETRY_SCHEMA_VERSION = 1
SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS = 900
SUMO_EDGE_TELEMETRY_PRODUCER_VERSION = "phase-2.1-v1"
SUMO_EDGE_TELEMETRY_METRIC_SEMANTICS_VERSION = "sumo-edge-data-v1"

_SHA256 = r"^[0-9a-f]{64}$"


class StrictTelemetryModel(BaseModel):
    """Forbid silent telemetry-contract expansion or misspelled fields."""

    model_config = ConfigDict(extra="forbid")


class TelemetryProducerIdentity(StrictTelemetryModel):
    name: Literal["sumo_native_edge_telemetry"]
    version: Literal[SUMO_EDGE_TELEMETRY_PRODUCER_VERSION]
    metric_semantics_version: Literal[
        SUMO_EDGE_TELEMETRY_METRIC_SEMANTICS_VERSION
    ]


class TelemetryFileIdentity(StrictTelemetryModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int = Field(ge=0)


class TelemetrySourceIdentity(StrictTelemetryModel):
    graph_version: str = Field(min_length=1, max_length=160)
    graph_manifest_sha256: str = Field(pattern=_SHA256)
    network_version: str = Field(min_length=1, max_length=160)
    network_sha256: str = Field(pattern=_SHA256)
    network_manifest_sha256: str = Field(pattern=_SHA256)
    sumo_version: str = Field(min_length=1, max_length=80)
    simulation_mode: Literal["mesoscopic"]
    seed: int = Field(ge=0)
    demand_version: str = Field(min_length=1, max_length=200)
    demand_routes_sha256: str = Field(pattern=_SHA256)
    real_vehicles_per_simulated_vehicle: float = Field(ge=1)


class TelemetryIntervalContract(StrictTelemetryModel):
    interval_seconds: Literal[SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS]
    interval_semantics: Literal["half_open_start_inclusive_end_exclusive"]
    simulation_start_seconds: int = Field(ge=0)
    simulation_end_seconds: int = Field(gt=0)
    complete_interval_count_per_variant: int = Field(ge=0)
    omitted_partial_seconds_at_start: int = Field(ge=0, lt=900)
    omitted_partial_seconds_at_end: int = Field(ge=0, lt=900)

    @model_validator(mode="after")
    def validate_window(self) -> TelemetryIntervalContract:
        if self.simulation_end_seconds <= self.simulation_start_seconds:
            raise ValueError("Telemetry simulation end must follow its start")
        return self


class SumoEdgeTelemetryRecordV1(StrictTelemetryModel):
    """One native SUMO edge in one complete half-open 900-second interval."""

    schema_version: Literal[SUMO_EDGE_TELEMETRY_SCHEMA_VERSION]
    run_identity: str = Field(min_length=1, max_length=160)
    network_version: str = Field(min_length=1, max_length=160)
    sumo_version: str = Field(min_length=1, max_length=80)
    simulation_mode: Literal["mesoscopic"]
    seed: int = Field(ge=0)
    demand_version: str = Field(min_length=1, max_length=200)
    variant: Literal["baseline", "scenario"]
    sumo_edge_id: str = Field(min_length=1, max_length=500)
    interval_start_seconds: int = Field(ge=0)
    interval_end_seconds: int = Field(gt=0)
    interval_duration_seconds: Literal[SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS]
    entered_count: float = Field(ge=0)
    departed_count: float = Field(ge=0)
    left_count: float = Field(ge=0)
    arrived_count: float = Field(ge=0)
    flow_vph: float = Field(ge=0)
    flow_derivation: Literal[
        "entered_count * 3600 / interval_duration_seconds"
    ]
    sampled_vehicle_seconds: float = Field(ge=0)
    mean_speed_mps: float | None = Field(default=None, ge=0)
    mean_speed_kph: float | None = Field(default=None, ge=0)
    mean_travel_time_seconds: float | None = Field(default=None, ge=0)
    density_veh_per_km: float | None = Field(default=None, ge=0)
    occupancy_percent: float | None = Field(default=None, ge=0)
    waiting_time_seconds: float | None = Field(default=None, ge=0)
    time_loss_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_interval_and_derivations(self) -> SumoEdgeTelemetryRecordV1:
        if self.interval_end_seconds - self.interval_start_seconds != 900:
            raise ValueError("SUMO edge telemetry intervals must be exactly 900 seconds")
        expected_flow = self.entered_count * 4.0
        if abs(self.flow_vph - expected_flow) > 1e-8:
            raise ValueError("flow_vph must be derived from entered_count over 900 seconds")
        if (self.mean_speed_mps is None) != (self.mean_speed_kph is None):
            raise ValueError("m/s and km/h mean speed must be present together")
        if self.mean_speed_mps is not None:
            expected_kph = self.mean_speed_mps * 3.6
            if abs(float(self.mean_speed_kph) - expected_kph) > 1e-7:
                raise ValueError("mean_speed_kph must equal mean_speed_mps times 3.6")
        return self


class SumoEdgeTelemetryManifestV1(StrictTelemetryModel):
    schema_version: Literal[SUMO_EDGE_TELEMETRY_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_sumo_native_edge_telemetry"]
    artifact_status: Literal["complete"]
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer: TelemetryProducerIdentity
    application_run_id: str = Field(min_length=1, max_length=160)
    run_identity: str = Field(min_length=1, max_length=160)
    source: TelemetrySourceIdentity
    interval: TelemetryIntervalContract
    variants: tuple[Literal["baseline", "scenario"], Literal["baseline", "scenario"]]
    edge_count_per_interval: int = Field(ge=1)
    native_fragment_count: int = Field(ge=1)
    native_source: Literal["SUMO edgeData"]
    flow_derivation: Literal[
        "entered_count * 3600 / interval_duration_seconds"
    ]
    output: TelemetryFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> SumoEdgeTelemetryManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.variants != ("baseline", "scenario"):
            raise ValueError("Regional telemetry must contain baseline then scenario")
        expected_rows = (
            self.edge_count_per_interval
            * self.interval.complete_interval_count_per_variant
            * len(self.variants)
        )
        if self.output.row_count != expected_rows:
            raise ValueError("Telemetry row count does not match its dense interval grain")
        return self
