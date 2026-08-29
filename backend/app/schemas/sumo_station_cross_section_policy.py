"""Typed Phase 2.2d station cross-section acceptance policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

POLICY_SCHEMA_VERSION = 1
POLICY_NAME = "historical_station_sumo_cross_section_policy"
POLICY_VERSION = "phase-2.2d-policy-v1"
POLICY_ALGORITHM_VERSION = "distance-boundary-direction-v1"
POLICY_SCHEMA_VERSION_V2 = 2
POLICY_VERSION_V2 = "phase-2.2d-policy-v2"
POLICY_DEPENDENCY_CONTRACT_V2 = (
    "inherit_v1_policy_semantics_recompute_comparison_v2_changed_dependencies"
)


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


class StationCrossSectionPolicyManifestV2(BaseModel):
    """Traffic-blind policy-v2 lineage over accepted cross-sections-v2-r3."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    artifact_type: Literal["commute_help_station_cross_section_acceptance_policy"]
    artifact_status: Literal["complete_policy"]
    calibration_status: Literal["not_calibrated"]
    policy_name: Literal["historical_station_sumo_cross_section_policy"]
    policy_version: Literal["phase-2.2d-policy-v2"]
    policy_algorithm_version: Literal["distance-boundary-direction-v1"]
    dependency_contract: Literal[
        "inherit_v1_policy_semantics_recompute_comparison_v2_changed_dependencies"
    ]
    row_schema_policy: Literal[
        "inherited_rows_retain_v1_identity_changed_rows_use_v2_identity"
    ]
    parent_policy_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_policy_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_policy_parquet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_station_cross_section_manifest_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    source_station_cross_section_validation_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    source_station_cross_section_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_station_cross_section_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_relation_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_relation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_relation_parquet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_version: str
    sumo_network_version: str
    generated_at: str
    decision_rules: dict[str, object]
    dependency_changed_station_ids: list[str]
    dependency_changed_app_edge_ids: list[str]
    inherited_station_count: int = Field(ge=0)
    recomputed_station_count: int = Field(ge=0)
    historical_coverage_summary_recomputed: Literal[False]
    policy_output: PolicyOutputV1
    review_output: PolicyOutputV1
    summary_output: PolicyOutputV1
    report_output: PolicyOutputV1
    row_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scientific_guards: dict[str, bool]
