"""Typed Phase 2.2f native SUMO departure-provenance contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEPARTURE_PROVENANCE_SCHEMA_VERSION = 2
DEPARTURE_PROVENANCE_PRODUCER = "sumo_random_free_departure_provenance_analyzer"
DEPARTURE_PROVENANCE_VERSION = "phase-2.2f-v2"
DEPARTURE_PROVENANCE_ALGORITHM = (
    "process-isolated-native-departure-fragment-reconciliation-v1"
)


class StrictDepartureProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartureProvenanceOutputV1(StrictDepartureProvenanceModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)


class DepartureProvenanceManifestV2(StrictDepartureProvenanceModel):
    schema_version: Literal[2]
    artifact_type: Literal["commute_help_random_free_departure_provenance_feasibility"]
    artifact_status: Literal["complete_feasibility"]
    calibration_status: Literal["not_calibrated"]
    producer_name: Literal["sumo_random_free_departure_provenance_analyzer"]
    producer_version: Literal["phase-2.2f-v2"]
    reconciliation_algorithm: Literal[
        "process-isolated-native-departure-fragment-reconciliation-v1"
    ]
    sumo_version: str
    simulation_mode: Literal["mesoscopic"]
    fixture_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_interval_seconds: Literal[100]
    evidence_interval_seconds: Literal[900]
    generated_at: str
    feasibility_output: DepartureProvenanceOutputV1
    report_output: DepartureProvenanceOutputV1
    validation_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
