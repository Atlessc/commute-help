"""Typed provenance contract for the Phase 1.4 legacy P95 tail analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from backend.app.schemas.calibration_v2 import (
    GeneratorIdentity,
    StrictCalibrationModel,
    _require_aware,
)

P95_TAIL_ANALYSIS_SCHEMA_VERSION = 1
P95_TAIL_ANALYSIS_VERSION = "phase-1.4-v1"
_DIGEST = r"^[0-9a-f]{64}$"


class P95TailFileIdentity(StrictCalibrationModel):
    relative_path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=_DIGEST)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class LegacyPooledMethodology(StrictCalibrationModel):
    weighting_grain: Literal["accepted_station_interval_observation"]
    source_months: tuple[Literal[9, 10], Literal[9, 10]]
    weekdays: Literal["monday_through_friday_pooled"]
    pm_window_start_minute: Literal[840]
    pm_window_end_minute_exclusive: Literal[1140]
    slowdown_definition: Literal[
        "app_edge_maxspeed_kph_divided_by_station_speed_kph_clipped_1_to_5"
    ]
    missing_speed: Literal["omitted_from_multiplier_distribution"]
    zero_speed: Literal["division_infinity_clipped_to_5_if_profile_eligible"]
    quantiles: Literal["numpy_linear"]
    stored_reliability_samples: Literal[
        "up_to_10000_evenly_spaced_order_statistics_rounded_6_decimals"
    ]


class ModernComparisonMethodology(StrictCalibrationModel):
    grain: Literal["app_edge_id_weekday_15_minute_bucket"]
    pm_window_start_minute: Literal[840]
    pm_window_end_minute_exclusive: Literal[1140]
    comparison_measure: Literal["within_profile_slowdown_p95"]
    cross_profile_quantiles: Literal["numpy_linear_equal_profile_weight"]
    support_sensitivity: Literal[
        "all_candidate_direct_plus_supplemental_and_direct_only"
    ]
    zero_speed_sensitivity: Literal[
        "compare_profiles_with_and_without_preserved_zero_speed_diagnostics"
    ]
    calibration_effect: Literal["none_descriptive_analysis_only"]


class P95TailAnalysisManifestV1(StrictCalibrationModel):
    schema_version: Literal[P95_TAIL_ANALYSIS_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_historical_p95_tail_analysis"]
    evidence_status: Literal["descriptive_analysis_only"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    analyzer: GeneratorIdentity
    methodology_version: Literal[P95_TAIL_ANALYSIS_VERSION]
    legacy_methodology: LegacyPooledMethodology
    modern_methodology: ModernComparisonMethodology
    source_phase_1_3_content_digest: str = Field(pattern=_DIGEST)
    source_quality_policy_content_digest: str = Field(pattern=_DIGEST)
    graph_version: str = Field(min_length=1, max_length=160)
    legacy_inputs: tuple[P95TailFileIdentity, ...]
    modern_profile_input: P95TailFileIdentity
    quality_characterization_input: P95TailFileIdentity
    quality_policy_input: P95TailFileIdentity
    graph_edges_input: P95TailFileIdentity
    output_profiles: P95TailFileIdentity
    output_summary: P95TailFileIdentity
    output_markdown: P95TailFileIdentity
    content_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_manifest(self) -> P95TailAnalysisManifestV1:
        _require_aware(self.generated_at, "generated_at")
        if not self.legacy_inputs:
            raise ValueError("P95 tail analysis requires legacy provenance")
        if self.output_profiles.row_count is None:
            raise ValueError("P95 tail profile output requires a row count")
        return self
