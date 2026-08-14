"""Typed Phase 2.2g station-cross-section PORTAL to SUMO comparison contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

COMPARATOR_V2_SCHEMA_VERSION = 1
COMPARATOR_V2_PRODUCER_NAME = "portal_sumo_station_flow_comparator"
COMPARATOR_V2_PRODUCER_VERSION = "phase-2.2g-v1"
COMPARATOR_V2_ALGORITHM = "station-cross-section-flow-v1"
COMPARATOR_V2_ELIGIBILITY_POLICY = "direct-complete-all-stations-v1"
HISTORICAL_STATION_PROFILE_VERSION = "phase-2.2g-station-flow-v1"

_SHA256 = r"^[0-9a-f]{64}$"


class StrictComparatorV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComparatorV2FileIdentity(StrictComparatorV2Model):
    relative_path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class HistoricalStationFlowManifestV1(StrictComparatorV2Model):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_historical_station_flow_profiles"]
    artifact_status: Literal["complete_validated_intermediate"]
    evidence_level: Literal["derived_historical_station_profile_input"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["historical_station_flow_profile_deriver"]
    producer_version: Literal["phase-2.2g-station-flow-v1"]
    aggregation_semantics: Literal[
        "sum_detectors_within_station_then_equal_date_weekday_distribution"
    ]
    source_corpus_digest: str = Field(pattern=_SHA256)
    source_integrity_digest: str = Field(pattern=_SHA256)
    source_historical_profile_digest: str = Field(pattern=_SHA256)
    source_date_level_sha256: str = Field(pattern=_SHA256)
    source_weekday_profile_sha256: str = Field(pattern=_SHA256)
    source_mapped_observation_count: int = Field(ge=0)
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    support_policy_version: Literal["phase-1.3-policy-v1"]
    profiles_output: ComparatorV2FileIdentity
    roundtrip_output: ComparatorV2FileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_station_manifest(self) -> HistoricalStationFlowManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return self


class ComparatorV2Manifest(StrictComparatorV2Model):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_portal_sumo_station_flow_comparison"]
    artifact_status: Literal["complete_diagnostic"]
    evidence_level: Literal["modeled_uncalibrated"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["portal_sumo_station_flow_comparator"]
    producer_version: Literal["phase-2.2g-v1"]
    comparator_algorithm: Literal["station-cross-section-flow-v1"]
    eligibility_policy_version: Literal["direct-complete-all-stations-v1"]
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    historical_corpus_digest: str = Field(pattern=_SHA256)
    historical_integrity_digest: str = Field(pattern=_SHA256)
    historical_profile_content_digest: str = Field(pattern=_SHA256)
    historical_quality_policy_digest: str = Field(pattern=_SHA256)
    historical_station_profile_version: Literal["phase-2.2g-station-flow-v1"]
    station_policy_content_digest: str = Field(pattern=_SHA256)
    observation_plan_digest: str = Field(pattern=_SHA256)
    station_telemetry_content_digest: str = Field(pattern=_SHA256)
    comparator_v1_content_digest: str | None = Field(default=None, pattern=_SHA256)
    comparator_v1_summary_sha256: str | None = Field(default=None, pattern=_SHA256)
    application_run_id: str
    run_identity: str
    graph_version: str
    network_version: str
    sumo_version: str
    variants: tuple[Literal["baseline", "scenario"], ...]
    station_profiles_output: ComparatorV2FileIdentity
    station_profiles_manifest: ComparatorV2FileIdentity
    station_roundtrip_output: ComparatorV2FileIdentity
    station_profile_content_digest: str = Field(pattern=_SHA256)
    station_comparisons_output: ComparatorV2FileIdentity
    app_edge_comparisons_output: ComparatorV2FileIdentity
    scoreboard_output: ComparatorV2FileIdentity
    report_output: ComparatorV2FileIdentity
    station_row_content_sha256: str = Field(pattern=_SHA256)
    app_edge_row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> ComparatorV2Manifest:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if not self.variants or len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must be nonempty and unique")
        return self
