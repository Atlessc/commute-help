"""Typed contract for Phase 1.3 historical evidence policy application."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from backend.app.schemas.calibration_v2 import (
    GeneratorIdentity,
    StrictCalibrationModel,
    _require_aware,
)

HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION = 1
HISTORICAL_QUALITY_POLICY_VERSION = "phase-1.3-policy-v1"
_DIGEST = r"^[0-9a-f]{64}$"

DateSupportClass = Literal[
    "direct_calibration_evidence",
    "supplemental_evidence",
    "insufficient_direct_evidence",
]
MeasurementEvidenceStatus = Literal[
    "direct_calibration_evidence",
    "supplemental_evidence",
    "insufficient_direct_evidence",
    "no_eligible_measurement_evidence",
]


class PolicyFileIdentity(StrictCalibrationModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_DIGEST)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class DateSupportPolicyV1(StrictCalibrationModel):
    possible_date_semantics: Literal["inclusive_source_window_calendar_by_weekday"]
    direct_minimum_coverage_fraction: Literal[0.9]
    supplemental_minimum_coverage_fraction: Literal[0.75]
    threshold_evaluation: Literal["exact_observed_over_possible_integer_ratio"]
    sparse_profiles: Literal["retain_never_delete"]


class FlowEvidencePolicyV1(StrictCalibrationModel):
    direct_requirement: Literal["direct_date_support_and_present_flow_evidence"]
    zero_flow: Literal["preserve_as_valid_numeric_evidence"]
    cross_measurement_anomalies: Literal["do_not_invalidate_flow"]


class SpeedEvidencePolicyV1(StrictCalibrationModel):
    direct_requirement: Literal[
        "direct_date_support_and_at_least_one_nonzero_numeric_speed"
    ]
    missing_speed: Literal["preserve_and_account"]
    zero_speed: Literal["preserve_as_diagnostic_no_automatic_profile_rejection"]
    slowdown_for_zero_speed: Literal["undefined"]
    zero_speed_threshold: Literal["none"]


class OccupancyEvidencePolicyV1(StrictCalibrationModel):
    direct_requirement: Literal[
        "direct_date_support_and_at_least_one_occupancy_not_above_100"
    ]
    above_100: Literal["preserve_but_exclude_only_from_occupancy_calibration"]
    repair: Literal["none_no_clipping"]
    cross_measurement_anomalies: Literal["do_not_invalidate_flow_or_speed"]


class SampleCountPolicyV1(StrictCalibrationModel):
    use: Literal["descriptive_support_and_anomaly_context_only"]
    global_minimum_threshold: None
    rationale: Literal[
        "aggregated_edge_date_bucket_countreadings_not_raw_detector_distribution"
    ]


class NetworkFallbackPolicyV1(StrictCalibrationModel):
    insufficient_history_means: Literal[
        "insufficient_for_direct_calibration_not_zero_traffic_or_closed_edge"
    ]
    downstream_requirement: Literal["use_separate_prior_or_model_fallback"]


class HistoricalQualityPolicyV1(StrictCalibrationModel):
    grain: Literal["app_edge_id_weekday_15_minute_bucket"]
    date_support: DateSupportPolicyV1
    flow: FlowEvidencePolicyV1
    speed: SpeedEvidencePolicyV1
    occupancy: OccupancyEvidencePolicyV1
    sample_count: SampleCountPolicyV1
    network_fallback: NetworkFallbackPolicyV1


class HistoricalQualityPolicyManifestV1(StrictCalibrationModel):
    schema_version: Literal[HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION]
    artifact_type: Literal[
        "commute_help_historical_calibration_quality_policy_application"
    ]
    artifact_status: Literal["quality_policy_applied_unvalidated"]
    calibration_status: Literal["not_calibrated"]
    source_profile_status: Literal["candidate_unvalidated"]
    generated_at: datetime
    compiler: GeneratorIdentity
    policy_version: Literal[HISTORICAL_QUALITY_POLICY_VERSION]
    policy: HistoricalQualityPolicyV1
    source_phase_1_3_content_digest: str = Field(pattern=_DIGEST)
    source_phase_1_3_manifest_sha256: str = Field(pattern=_DIGEST)
    source_characterization_content_digest: str = Field(pattern=_DIGEST)
    source_characterization_manifest_sha256: str = Field(pattern=_DIGEST)
    graph_version: str = Field(min_length=1, max_length=160)
    input_profile_characterization: PolicyFileIdentity
    input_characterization_report: PolicyFileIdentity
    output_profile_status: PolicyFileIdentity
    output_report_json: PolicyFileIdentity
    output_report_markdown: PolicyFileIdentity
    content_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_manifest(self) -> HistoricalQualityPolicyManifestV1:
        _require_aware(self.generated_at, "generated_at")
        if self.output_profile_status.row_count is None:
            raise ValueError("Policy profile-status output requires a row count")
        return self
