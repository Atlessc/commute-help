"""Typed Phase 2.2d station cross-section acceptance policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

POLICY_SCHEMA_VERSION = 1
POLICY_NAME = "historical_station_sumo_cross_section_policy"
POLICY_VERSION = "phase-2.2d-policy-v1"
POLICY_ALGORITHM_VERSION = "distance-boundary-direction-v1"


class PolicyOutputV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class StationCrossSectionPolicyManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    artifact_type: Literal["commute_help_station_cross_section_acceptance_policy"]
    artifact_status: Literal["complete_policy"]
    calibration_status: Literal["not_calibrated"]
    policy_name: Literal["historical_station_sumo_cross_section_policy"]
    policy_version: Literal["phase-2.2d-policy-v1"]
    policy_algorithm_version: Literal["distance-boundary-direction-v1"]
    source_station_cross_section_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_station_cross_section_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_edge_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_relation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_version: str
    sumo_network_version: str
    generated_at: str
    decision_rules: dict[str, object]
    policy_output: PolicyOutputV1
    review_output: PolicyOutputV1
    summary_output: PolicyOutputV1
    report_output: PolicyOutputV1
    row_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
