"""Typed Phase 2.2a app-edge to SUMO comparison-relation contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUMO_COMPARISON_RELATION_SCHEMA_VERSION = 1
SUMO_COMPARISON_RELATION_PRODUCER_VERSION = "phase-2.2a-v1"
SUMO_COMPARISON_RELATION_ALGORITHM_VERSION = "directed-member-topology-v1"
SUMO_COMPARISON_RELATION_SCHEMA_VERSION_V2 = 2
SUMO_COMPARISON_RELATION_PRODUCER_VERSION_V2 = "phase-2.2a-v2"
SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2 = (
    "directed-member-topology-synthetic-boundary-v2"
)
SUMO_COMPARISON_RELATION_REPAIR_REASON_V2 = (
    "r3_v2_diagnostic_comparison_chain_boundary_omits_synthetic_external_sumo_segment"
)

RelationClass = Literal[
    "single_edge",
    "ordered_linear_chain",
    "ordered_chain_with_side_connections",
    "parallel_candidates",
    "branching_candidates",
    "disconnected_candidates",
    "cycle_candidates",
    "direction_conflict",
    "identity_conflict",
    "unresolved",
]

_SHA256 = r"^[0-9a-f]{64}$"


class StrictRelationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationFileIdentity(StrictRelationModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class ComparisonRelationManifestV1(StrictRelationModel):
    schema_version: Literal[SUMO_COMPARISON_RELATION_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_app_sumo_comparison_relation_resolution"]
    artifact_status: Literal["complete_analysis"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["app_sumo_comparison_relation_resolver"]
    producer_version: Literal[SUMO_COMPARISON_RELATION_PRODUCER_VERSION]
    resolution_algorithm_version: Literal[
        SUMO_COMPARISON_RELATION_ALGORITHM_VERSION
    ]
    graph_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    original_edge_map_sha256: str = Field(pattern=_SHA256)
    sumo_network_sha256: str = Field(pattern=_SHA256)
    historical_profile_content_digest: str = Field(pattern=_SHA256)
    quality_policy_content_digest: str = Field(pattern=_SHA256)
    detector_location_semantics: Literal[
        "station_points_exist_but_no_versioned_station_to_sumo_segment_projection"
    ]
    flow_semantics: Literal[
        "chain_entrance_entered_plus_departed_approximation_not_detector_aligned"
    ]
    speed_semantics: Literal["total_chain_distance_divided_by_sum_segment_travel_time"]
    slowdown_semantics: Literal[
        "sum_segment_observed_travel_time_divided_by_sum_segment_reference_travel_time"
    ]
    relation_output: RelationFileIdentity
    summary_output: RelationFileIdentity
    report_output: RelationFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> ComparisonRelationManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.relation_output.row_count is None:
            raise ValueError("relation Parquet requires a row count")
        return self


class ComparisonRelationManifestV2(StrictRelationModel):
    schema_version: Literal[SUMO_COMPARISON_RELATION_SCHEMA_VERSION_V2]
    artifact_type: Literal["commute_help_app_sumo_comparison_relation_resolution"]
    artifact_status: Literal["complete_analysis"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["app_sumo_comparison_relation_boundary_repair"]
    producer_version: Literal[SUMO_COMPARISON_RELATION_PRODUCER_VERSION_V2]
    resolution_algorithm_version: Literal[
        SUMO_COMPARISON_RELATION_ALGORITHM_VERSION_V2
    ]
    repair_reason: Literal[SUMO_COMPARISON_RELATION_REPAIR_REASON_V2]
    graph_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    original_edge_map_sha256: str = Field(pattern=_SHA256)
    graph_edges_sha256: str = Field(pattern=_SHA256)
    sumo_network_sha256: str = Field(pattern=_SHA256)
    parent_relation_manifest_sha256: str = Field(pattern=_SHA256)
    parent_relation_content_digest: str = Field(pattern=_SHA256)
    parent_relation_parquet_sha256: str = Field(pattern=_SHA256)
    repair_evidence_summary_sha256: str = Field(pattern=_SHA256)
    repair_evidence_provenance_sha256: str = Field(pattern=_SHA256)
    changed_relation_count: int = Field(ge=1)
    synthetic_boundary_member_count: int = Field(ge=1)
    relation_output: RelationFileIdentity
    summary_output: RelationFileIdentity
    report_output: RelationFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> ComparisonRelationManifestV2:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.relation_output.row_count is None:
            raise ValueError("relation Parquet requires a row count")
        return self
