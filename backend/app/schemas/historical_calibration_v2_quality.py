"""Typed provenance for descriptive Phase 1.3 quality characterization."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from backend.app.schemas.calibration_v2 import (
    GeneratorIdentity,
    StrictCalibrationModel,
    _require_aware,
)

QUALITY_CHARACTERIZATION_SCHEMA_VERSION = 1
_DIGEST = r"^[0-9a-f]{64}$"


class CharacterizationFileIdentity(StrictCalibrationModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_DIGEST)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class QualityCharacterizationMethodology(StrictCalibrationModel):
    grain: Literal["app_edge_id_weekday_15_minute_bucket"]
    possible_dates: Literal["inclusive_source_window_calendar"]
    measurement_missingness: Literal["possible_dates_minus_present_measurement_dates"]
    quantiles: Literal["exact_linear_within_profile"]
    aggregation: Literal["descriptive_only_no_eligibility_policy"]
    edge_partitioning: Literal["contiguous_sorted_edge_ranges"]


class QualityCharacterizationManifestV1(StrictCalibrationModel):
    schema_version: Literal[QUALITY_CHARACTERIZATION_SCHEMA_VERSION]
    artifact_type: Literal[
        "commute_help_historical_calibration_quality_characterization"
    ]
    evidence_status: Literal["descriptive_characterization_only"]
    calibration_status: Literal["not_calibrated"]
    source_profile_status: Literal["candidate_unvalidated"]
    generated_at: datetime
    analyzer: GeneratorIdentity
    methodology_version: Literal["phase-1.3-quality-v1"]
    methodology: QualityCharacterizationMethodology
    source_phase_1_3_manifest_sha256: str = Field(pattern=_DIGEST)
    source_phase_1_3_content_digest: str = Field(pattern=_DIGEST)
    source_corpus_digest: str = Field(pattern=_DIGEST)
    source_integrity_digest: str = Field(pattern=_DIGEST)
    graph_version: str = Field(min_length=1, max_length=160)
    input_date_level: CharacterizationFileIdentity
    input_weekday_profiles: CharacterizationFileIdentity
    output_profile_characterization: CharacterizationFileIdentity
    output_json: CharacterizationFileIdentity
    output_markdown: CharacterizationFileIdentity
    content_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_manifest(self) -> QualityCharacterizationManifestV1:
        _require_aware(self.generated_at, "generated_at")
        if self.output_profile_characterization.row_count is None:
            raise ValueError("Profile characterization output requires a row count")
        return self
