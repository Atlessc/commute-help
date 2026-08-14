"""Typed envelope for Phase 1.3 bounded historical calibration artifacts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from backend.app.schemas.calibration_v2 import (
    GeneratorIdentity,
    StrictCalibrationModel,
    Weekday,
    _require_aware,
)

HISTORICAL_CALIBRATION_COMPILER_SCHEMA_VERSION = 1
_DIGEST = r"^[0-9a-f]{64}$"
_ID = r"^[a-z0-9][a-z0-9._:-]{2,159}$"


class CompiledTableIdentity(StrictCalibrationModel):
    relative_path: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+\.parquet$")
    schema_version: Literal[1]
    sha256: str = Field(pattern=_DIGEST)
    byte_count: int = Field(ge=0)
    row_count: int = Field(ge=0)


class HistoricalAggregationPolicy(StrictCalibrationModel):
    detector_to_station_flow: Literal["sum_detector_counts"]
    detector_to_station_speed: Literal["positive_volume_weighted_else_median"]
    detector_to_station_occupancy: Literal["arithmetic_mean_present"]
    station_to_edge: Literal["median_across_stations"]
    missing_measurements: Literal["retain_null_no_imputation"]
    outliers: Literal["preserve_source_values_no_threshold"]
    weekday_aggregation: Literal["exact_date_equal_weight"]
    quantiles: Literal["exact_partition_bounded"]
    profile_promotion: Literal["candidate_unvalidated"]


class HistoricalCalibrationCompilerManifestV1(StrictCalibrationModel):
    schema_version: Literal[HISTORICAL_CALIBRATION_COMPILER_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_historical_calibration_compilation"]
    artifact_id: str = Field(pattern=_ID)
    artifact_status: Literal["historical_input"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    compiler: GeneratorIdentity
    source_corpus_digest: str = Field(pattern=_DIGEST)
    source_integrity_digest: str = Field(pattern=_DIGEST)
    source_campaign_manifest_sha256: str = Field(pattern=_DIGEST)
    graph_version: str = Field(min_length=1, max_length=160)
    graph_edges_sha256: str = Field(pattern=_DIGEST)
    source_window_start: date
    source_window_end: date
    timezone: Literal["America/Los_Angeles"]
    interval_seconds: Literal[900]
    weekdays: tuple[Weekday, ...]
    possible_buckets_per_day: Literal[96]
    complete_calendar_required: bool
    edge_partition_count: int = Field(ge=1)
    json_block_size_bytes: int = Field(ge=65_536)
    aggregation_policy: HistoricalAggregationPolicy
    source_observation_count: int = Field(ge=0)
    mapped_observation_count: int = Field(ge=0)
    unmapped_observation_count: int = Field(ge=0)
    date_level: CompiledTableIdentity
    weekday_profiles: CompiledTableIdentity
    content_digest: str = Field(pattern=_DIGEST)

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        expected = ("monday", "tuesday", "wednesday", "thursday", "friday")
        if value != expected:
            raise ValueError("Historical compiler must preserve exactly Monday-Friday")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> HistoricalCalibrationCompilerManifestV1:
        _require_aware(self.generated_at, "generated_at")
        if self.source_window_end < self.source_window_start:
            raise ValueError("Historical compiler source window is invalid")
        if (
            self.mapped_observation_count + self.unmapped_observation_count
            != self.source_observation_count
        ):
            raise ValueError(
                "Mapped and unmapped observations must explain source rows"
            )
        return self
