"""Typed Phase 2.2 PORTAL historical evidence to SUMO comparison contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUMO_HISTORICAL_COMPARISON_SCHEMA_VERSION = 1
SUMO_HISTORICAL_COMPARISON_PRODUCER_VERSION = "phase-2.2-v1"
SUMO_HISTORICAL_MAPPING_CONTRACT_VERSION = "accepted-one-to-one-v1"
SUMO_HISTORICAL_FLOW_SEMANTICS_VERSION = "edge-inflow-entered-plus-departed-v1"

_SHA256 = r"^[0-9a-f]{64}$"


class StrictComparisonModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComparisonProducerIdentity(StrictComparisonModel):
    name: Literal["portal_sumo_edge_time_comparator"]
    version: Literal[SUMO_HISTORICAL_COMPARISON_PRODUCER_VERSION]


class ComparisonFileIdentity(StrictComparisonModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class ComparisonMappingIdentity(StrictComparisonModel):
    contract_version: Literal[SUMO_HISTORICAL_MAPPING_CONTRACT_VERSION]
    graph_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    edge_map_sha256: str = Field(pattern=_SHA256)
    accepted_relation_semantics: Literal[
        "exactly_one_distinct_accepted_sumo_edge_per_app_edge"
    ]
    ambiguity_semantics: Literal["exclude_without_selecting_a_winner"]


class ComparisonFlowContract(StrictComparisonModel):
    semantics_version: Literal[SUMO_HISTORICAL_FLOW_SEMANTICS_VERSION]
    historical_source: Literal["PORTAL_15_minute_detector_count_aggregated_to_edge"]
    sumo_comparable_count: Literal["entered_count_plus_departed_count"]
    sumo_comparable_flow: Literal[
        "(entered_count + departed_count) * 3600 / interval_duration_seconds"
    ]
    native_phase_2_1_flow_preserved: Literal[True]
    limitation: Literal[
        "SUMO_edge_inflow_is_not_identical_to_a_PORTAL_point_detector_crossing"
    ]


class SumoHistoricalComparisonManifestV1(StrictComparisonModel):
    schema_version: Literal[SUMO_HISTORICAL_COMPARISON_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_portal_sumo_edge_time_comparison"]
    artifact_status: Literal["complete_diagnostic"]
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer: ComparisonProducerIdentity
    application_run_id: str = Field(min_length=1, max_length=160)
    run_identity: str = Field(min_length=1, max_length=160)
    telemetry_content_digest: str = Field(pattern=_SHA256)
    historical_profile_content_digest: str = Field(pattern=_SHA256)
    quality_policy_content_digest: str = Field(pattern=_SHA256)
    mapping: ComparisonMappingIdentity
    flow_contract: ComparisonFlowContract
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    compared_weekdays: list[
        Literal["monday", "tuesday", "wednesday", "thursday", "friday"]
    ]
    compared_bucket_start_minutes: list[int]
    variants: list[Literal["baseline", "scenario"]]
    comparison_output: ComparisonFileIdentity
    summary_output: ComparisonFileIdentity
    report_output: ComparisonFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> SumoHistoricalComparisonManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if not self.variants or len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must be nonempty and unique")
        if any(value < 0 or value >= 1440 or value % 15 for value in self.compared_bucket_start_minutes):
            raise ValueError("comparison buckets must be 15-minute local-day starts")
        return self
