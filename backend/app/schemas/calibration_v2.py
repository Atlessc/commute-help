"""Canonical typed contracts for calibration-v2 historical traffic artifacts."""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

CALIBRATION_V2_SCHEMA_VERSION = 2
CALIBRATION_V2_INTERVAL_SECONDS = 900

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday"]
Direction = Literal[
    "northbound",
    "southbound",
    "eastbound",
    "westbound",
    "bidirectional",
    "unknown",
]
MeasurementField = Literal[
    "volume_count",
    "flow_vph",
    "speed_kph",
    "occupancy_percent",
]
MetricUnit = Literal[
    "vehicles_per_interval",
    "vehicles_per_hour",
    "kilometers_per_hour",
    "percent",
    "ratio",
]

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday")
_ID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{2,159}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictCalibrationModel(BaseModel):
    """Reject unknown fields so version changes cannot pass silently."""

    model_config = ConfigDict(extra="forbid")


class GeneratorIdentity(StrictCalibrationModel):
    name: str = Field(min_length=1, max_length=120)
    code_version: str = Field(min_length=1, max_length=120)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)


class SourceDatasetIdentity(StrictCalibrationModel):
    dataset_id: str = Field(pattern=_ID_PATTERN)
    dataset_version: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=160)
    synthetic: bool


class SourceReference(StrictCalibrationModel):
    reference_kind: Literal["file", "manifest"]
    reference_id: str = Field(pattern=_ID_PATTERN)
    reference_version: str | None = Field(default=None, min_length=1, max_length=160)
    relative_path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("Source relative_path must use POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or value != str(path)
        ):
            raise ValueError("Source relative_path must be a normalized relative path")
        return str(path)


class EvidenceIdentity(StrictCalibrationModel):
    input_status: Literal[
        "historical_observation_input",
        "derived_historical_profile_input",
        "synthetic_fixture",
    ]
    calibration_status: Literal["not_calibrated"]


class SourceLocation(StrictCalibrationModel):
    source_location_id: str = Field(min_length=1, max_length=160)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    crs: Literal["EPSG:4326"] = "EPSG:4326"
    route_id: str | None = Field(default=None, max_length=120)
    road_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_coordinate_pair(self) -> SourceLocation:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Source latitude and longitude must be provided together")
        return self


class DetectorIdentity(StrictCalibrationModel):
    station_id: str = Field(min_length=1, max_length=160)
    detector_id: str = Field(min_length=1, max_length=160)
    lane_id: str | None = Field(default=None, max_length=160)
    source_direction_code: str | None = Field(default=None, max_length=40)


class AcceptedSumoEdgeAssociation(StrictCalibrationModel):
    status: Literal["accepted"]
    mapping_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    sumo_edge_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("sumo_edge_ids")
    @classmethod
    def canonical_sumo_edge_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "SUMO edge IDs")


class RoadNetworkAssociation(StrictCalibrationModel):
    graph_version: str = Field(min_length=1, max_length=160)
    app_edge_id: str = Field(min_length=1, max_length=200)
    osm_way_ids: tuple[str, ...] = Field(default_factory=tuple)
    accepted_sumo_association: AcceptedSumoEdgeAssociation | None = None

    @field_validator("osm_way_ids")
    @classmethod
    def canonical_osm_way_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "OSM way IDs", allow_empty=True)


class ObservationCalendarIdentity(StrictCalibrationModel):
    exact_date: date
    weekday: Weekday
    timezone: str = Field(min_length=1, max_length=80)
    interval_start: datetime
    interval_end: datetime
    interval_seconds: Literal[CALIBRATION_V2_INTERVAL_SECONDS]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        _zone(value)
        return value

    @model_validator(mode="after")
    def validate_calendar(self) -> ObservationCalendarIdentity:
        zone = _zone(self.timezone)
        _require_aware(self.interval_start, "interval_start")
        _require_aware(self.interval_end, "interval_end")
        if self.interval_end <= self.interval_start:
            raise ValueError("Observation interval_end must be after interval_start")
        if self.interval_start.utcoffset() != self.interval_start.astimezone(zone).utcoffset():
            raise ValueError("interval_start offset does not match the declared timezone")
        if self.interval_end.utcoffset() != self.interval_end.astimezone(zone).utcoffset():
            raise ValueError("interval_end offset does not match the declared timezone")
        local_start = self.interval_start.astimezone(zone)
        if local_start.date() != self.exact_date:
            raise ValueError("exact_date must match interval_start in the declared timezone")
        expected_weekday = _weekday_for(self.exact_date)
        if self.weekday != expected_weekday:
            raise ValueError(
                f"weekday {self.weekday!r} does not match exact_date {self.exact_date}"
            )
        duration = int((self.interval_end - self.interval_start).total_seconds())
        if duration != self.interval_seconds:
            raise ValueError("Observation interval duration must equal interval_seconds")
        return self


class SeasonalProfileIdentity(StrictCalibrationModel):
    profile_id: str = Field(pattern=_ID_PATTERN)
    profile_version: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=200)
    season_name: str | None = Field(default=None, max_length=120)


class ProfileCalendarIdentity(StrictCalibrationModel):
    weekday: Weekday
    timezone: str = Field(min_length=1, max_length=80)
    bucket_start_minute: int = Field(ge=0, lt=1440)
    bucket_end_minute: int = Field(gt=0, le=1440)
    interval_seconds: Literal[CALIBRATION_V2_INTERVAL_SECONDS]
    profile: SeasonalProfileIdentity

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        _zone(value)
        return value

    @model_validator(mode="after")
    def validate_bucket(self) -> ProfileCalendarIdentity:
        if self.bucket_start_minute % 15 or self.bucket_end_minute % 15:
            raise ValueError("Profile buckets must use 15-minute boundaries")
        if self.bucket_end_minute - self.bucket_start_minute != 15:
            raise ValueError("Profile bucket end must be 15 minutes after its start")
        return self


class MeasurementUnits(StrictCalibrationModel):
    volume_count: Literal["vehicles_per_interval"]
    flow_vph: Literal["vehicles_per_hour"]
    speed: Literal["kilometers_per_hour"]
    occupancy: Literal["percent"]


class ObservationMeasurements(StrictCalibrationModel):
    volume_count: float | None = Field(default=None, ge=0)
    flow_vph: float | None = Field(default=None, ge=0)
    speed_kph: float | None = Field(default=None, ge=0)
    occupancy_percent: float | None = Field(default=None, ge=0)
    sample_count: int = Field(ge=0)
    units: MeasurementUnits


class ObservationQuality(StrictCalibrationModel):
    disposition: Literal["accepted", "rejected"]
    source_status: Literal["valid", "provisional", "invalid", "missing", "unknown"]
    confidence: Literal["high", "medium", "low", "unknown"]
    missing_fields: tuple[MeasurementField, ...] = Field(default_factory=tuple)
    quality_flags: tuple[str, ...] = Field(default_factory=tuple)
    exclusion_reasons: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("missing_fields")
    @classmethod
    def canonical_missing_fields(
        cls, value: tuple[MeasurementField, ...]
    ) -> tuple[MeasurementField, ...]:
        return tuple(_canonical_strings(value, "missing measurement fields", allow_empty=True))

    @field_validator("quality_flags")
    @classmethod
    def canonical_quality_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "quality flags", allow_empty=True)

    @field_validator("exclusion_reasons")
    @classmethod
    def canonical_exclusion_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "exclusion reasons", allow_empty=True)

    @model_validator(mode="after")
    def validate_disposition(self) -> ObservationQuality:
        if self.disposition == "accepted" and self.exclusion_reasons:
            raise ValueError("Accepted observations cannot have exclusion reasons")
        if self.disposition == "rejected" and not self.exclusion_reasons:
            raise ValueError("Rejected observations require an exclusion reason")
        return self


class CalibrationRecordBase(StrictCalibrationModel):
    record_id: str = Field(pattern=_ID_PATTERN)
    direction: Direction
    evidence: EvidenceIdentity


class ObservationRecord(CalibrationRecordBase):
    record_kind: Literal["observation"]
    detector: DetectorIdentity
    source_location: SourceLocation
    calendar: ObservationCalendarIdentity
    road_association: RoadNetworkAssociation | None = None
    measurements: ObservationMeasurements
    quality: ObservationQuality

    @model_validator(mode="after")
    def validate_measurement_quality(self) -> ObservationRecord:
        values = {
            "volume_count": self.measurements.volume_count,
            "flow_vph": self.measurements.flow_vph,
            "speed_kph": self.measurements.speed_kph,
            "occupancy_percent": self.measurements.occupancy_percent,
        }
        missing = set(self.quality.missing_fields)
        for field_name, value in values.items():
            if value is None and field_name not in missing:
                raise ValueError(f"Missing {field_name} must be declared in missing_fields")
            if value is not None and field_name in missing:
                raise ValueError(f"Present {field_name} cannot be declared missing")
        if self.quality.disposition == "accepted":
            if self.measurements.sample_count < 1:
                raise ValueError("Accepted observations require sample_count >= 1")
            if all(value is None for value in values.values()):
                raise ValueError("Accepted observations require at least one measurement")
        return self


class AggregationPolicy(StrictCalibrationModel):
    detector_collapse: Literal[
        "one_record_per_detector",
        "median_across_detectors",
        "sum_across_detectors",
    ]
    lane_collapse: Literal[
        "already_station_aggregated",
        "sum_lane_counts",
        "volume_weighted_speed",
    ]
    missing_bucket: Literal["exclude", "retain_missing"]
    outlier: Literal["source_quality_only", "iqr_filter", "winsorized"]
    aggregation_method: Literal["exact_quantiles", "deterministic_histogram"]
    minimum_sample_days: int = Field(ge=1)


class DerivedProfileProvenance(StrictCalibrationModel):
    source_artifact_ids: tuple[str, ...] = Field(min_length=1)
    source_record_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_station_ids: tuple[str, ...] = Field(min_length=1)
    source_window_start: date
    source_window_end: date
    observation_count: int = Field(ge=1)
    sample_days: int = Field(ge=1)
    policy: AggregationPolicy

    @field_validator("source_artifact_ids", "source_record_ids", "source_station_ids")
    @classmethod
    def canonical_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "profile provenance IDs", allow_empty=True)

    @model_validator(mode="after")
    def validate_source_window(self) -> DerivedProfileProvenance:
        if self.source_window_end < self.source_window_start:
            raise ValueError("Profile source window end must not precede its start")
        if self.sample_days > self.observation_count:
            raise ValueError("sample_days cannot exceed observation_count")
        return self


class MetricDistribution(StrictCalibrationModel):
    unit: MetricUnit
    sample_count: int = Field(ge=1)
    mean: float | None = Field(default=None, ge=0)
    p10: float | None = Field(default=None, ge=0)
    p50: float | None = Field(default=None, ge=0)
    p85: float | None = Field(default=None, ge=0)
    p90: float | None = Field(default=None, ge=0)
    p95: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_distribution(self) -> MetricDistribution:
        if all(
            value is None
            for value in (self.mean, self.p10, self.p50, self.p85, self.p90, self.p95)
        ):
            raise ValueError("A metric distribution requires at least one statistic")
        ordered = [
            value
            for value in (self.p10, self.p50, self.p85, self.p90, self.p95)
            if value is not None
        ]
        if any(right < left for left, right in pairwise(ordered)):
            raise ValueError("Metric quantiles must be nondecreasing")
        return self


class ProfileMeasurements(StrictCalibrationModel):
    volume: MetricDistribution | None = None
    speed: MetricDistribution | None = None
    occupancy: MetricDistribution | None = None
    slowdown: MetricDistribution | None = None

    @model_validator(mode="after")
    def require_measurement(self) -> ProfileMeasurements:
        if all(
            value is None
            for value in (self.volume, self.speed, self.occupancy, self.slowdown)
        ):
            raise ValueError("A profile requires at least one measurement distribution")
        return self


class ProfileQuality(StrictCalibrationModel):
    status: Literal["candidate", "excluded"]
    quality_flags: tuple[str, ...] = Field(default_factory=tuple)
    exclusion_reasons: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("quality_flags", "exclusion_reasons")
    @classmethod
    def canonical_profile_quality(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_strings(value, "profile quality values", allow_empty=True)

    @model_validator(mode="after")
    def validate_status(self) -> ProfileQuality:
        if self.status == "candidate" and self.exclusion_reasons:
            raise ValueError("Candidate profiles cannot have exclusion reasons")
        if self.status == "excluded" and not self.exclusion_reasons:
            raise ValueError("Excluded profiles require an exclusion reason")
        return self


class DerivedProfileRecord(CalibrationRecordBase):
    record_kind: Literal["derived_profile"]
    calendar: ProfileCalendarIdentity
    road_association: RoadNetworkAssociation
    provenance: DerivedProfileProvenance
    measurements: ProfileMeasurements
    quality: ProfileQuality


CalibrationRecord = Annotated[
    ObservationRecord | DerivedProfileRecord,
    Field(discriminator="record_kind"),
]


class CalibrationArtifactV2(StrictCalibrationModel):
    schema_version: Literal[CALIBRATION_V2_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_calibration"]
    artifact_id: str = Field(pattern=_ID_PATTERN)
    artifact_status: Literal["historical_input", "synthetic_fixture"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    generator: GeneratorIdentity
    source_dataset: SourceDatasetIdentity
    source_references: list[SourceReference] = Field(min_length=1)
    records: list[CalibrationRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_artifact(self) -> CalibrationArtifactV2:
        _require_aware(self.generated_at, "generated_at")
        reference_ids = [reference.reference_id for reference in self.source_references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("Source reference IDs must be unique")
        record_ids = [record.record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Calibration record IDs must be unique")
        if self.artifact_status == "synthetic_fixture" and not self.source_dataset.synthetic:
            raise ValueError("Synthetic fixtures require a synthetic source dataset")
        if self.artifact_status == "historical_input" and self.source_dataset.synthetic:
            raise ValueError("Historical input artifacts cannot use a synthetic source dataset")
        self.source_references = sorted(
            self.source_references, key=lambda value: value.reference_id
        )
        self.records = sorted(self.records, key=lambda value: value.record_id)
        return self


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown IANA timezone: {value}") from error


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")


def _weekday_for(value: date) -> str:
    if value.weekday() >= len(_WEEKDAY_NAMES):
        raise ValueError("Calibration-v2 historical records cannot use weekends")
    return _WEEKDAY_NAMES[value.weekday()]


def _canonical_strings(
    values: tuple[str, ...], label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    normalized = tuple(sorted(str(value).strip() for value in values))
    if not allow_empty and not normalized:
        raise ValueError(f"{label} cannot be empty")
    if any(not value for value in normalized):
        raise ValueError(f"{label} cannot contain blank values")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must be unique")
    return normalized
