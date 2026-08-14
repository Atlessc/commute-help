"""Typed Phase 2.2c mesoscopic station-crossing feasibility contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

STATION_CROSSING_SCHEMA_VERSION = 1
STATION_CROSSING_OBSERVER = "mesoscopic_vehicle_position_crossing_observer"
STATION_CROSSING_OBSERVER_VERSION = "phase-2.2c-v1"
STATION_CROSSING_ALGORITHM_VERSION = "ordered-position-transition-v1"


class FeasibilityOutputV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)


class StationCrossingFeasibilityManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = STATION_CROSSING_SCHEMA_VERSION
    artifact_type: Literal["commute_help_station_crossing_counter_feasibility"]
    artifact_status: Literal["complete_feasibility"]
    calibration_status: Literal["not_calibrated"]
    producer_name: Literal["mesoscopic_station_crossing_feasibility_analyzer"]
    producer_version: Literal["phase-2.2c-v1"]
    observer_name: Literal["mesoscopic_vehicle_position_crossing_observer"]
    observer_algorithm_version: Literal["ordered-position-transition-v1"]
    sumo_version: str
    simulation_mode: Literal["mesoscopic"]
    fixture_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    feasibility_semantic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_interval_seconds: Literal[100]
    evidence_interval_seconds: Literal[900]
    generated_at: str
    feasibility_output: FeasibilityOutputV1
    report_output: FeasibilityOutputV1
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
