"""Typed manifests for streamed calibration-v2 observation partitions.

``CalibrationArtifactV2`` remains the contract for an in-memory artifact.  A
historical corpus is intentionally represented by these small manifests and
canonical JSONL streams, so a 71M-row campaign never has to become one Python
object merely to validate its index.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from backend.app.schemas.calibration_v2 import (
    CALIBRATION_V2_SCHEMA_VERSION,
    GeneratorIdentity,
    SourceDatasetIdentity,
    SourceReference,
    StrictCalibrationModel,
    Weekday,
    _require_aware,
)

CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION = 1
CALIBRATION_V2_CORPUS_SCHEMA_VERSION = 2
_DIGEST = r"^[0-9a-f]{64}$"
_ID = r"^[a-z0-9][a-z0-9._:-]{2,159}$"


class ObservationStream(StrictCalibrationModel):
    relative_path: Literal["observations.jsonl", "observations.jsonl.gz"]
    compression: Literal["none", "gzip"]
    canonical_sha256: str = Field(pattern=_DIGEST)
    physical_sha256: str = Field(pattern=_DIGEST)
    canonical_byte_count: int = Field(ge=0)
    physical_byte_count: int = Field(ge=0)
    record_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_stream_encoding(self) -> ObservationStream:
        expected_path = "observations.jsonl.gz" if self.compression == "gzip" else "observations.jsonl"
        if self.relative_path != expected_path:
            raise ValueError("Observation stream path must agree with compression")
        return self


class ImportDiagnostic(StrictCalibrationModel):
    """Bounded evidence: hashes and coordinates, never a raw row dump."""

    reason: str = Field(min_length=1, max_length=120)
    source_reference_id: str = Field(pattern=_ID)
    line_number: int = Field(ge=2)
    row_sha256: str = Field(pattern=_DIGEST)
    observation_id: str | None = Field(default=None, pattern=_ID)
    related_payload_sha256: str | None = Field(default=None, pattern=_DIGEST)


class ImportAudit(StrictCalibrationModel):
    rows_encountered: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    export_boundary_suppressed_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    rejected_by_reason: dict[str, int] = Field(default_factory=dict)
    diagnostics: tuple[ImportDiagnostic, ...] = Field(default_factory=tuple, max_length=25)

    @field_validator("rejected_by_reason")
    @classmethod
    def validate_rejection_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or count < 1 for key, count in value.items()):
            raise ValueError("Rejection reasons must be nonblank positive counts")
        return dict(sorted(value.items()))


class MappingCoverage(StrictCalibrationModel):
    accepted_app_edge_attached_count: int = Field(ge=0)
    review_or_unmatched_not_attached_count: int = Field(ge=0)
    absent_mapping_count: int = Field(ge=0)


class CorpusIntegritySummary(StrictCalibrationModel):
    indexed_shard_count: int = Field(ge=0)
    physical_observation_count: int = Field(ge=0)
    logical_observation_count: int = Field(ge=0)
    global_duplicate_claim_count: int = Field(ge=0)
    global_conflict_identity_count: int = Field(ge=0)
    index_schema_version: Literal[2]
    index_digest: str = Field(pattern=_DIGEST)


class CorpusIntegrityProgress(StrictCalibrationModel):
    indexed_shard_count: int = Field(ge=0)
    physical_observation_count: int = Field(ge=0)
    logical_observation_count: int = Field(ge=0)
    global_duplicate_claim_count: int = Field(ge=0)
    global_conflict_identity_count: int = Field(ge=0)
    index_schema_version: Literal[2]


class IntegrityReconciliationIdentity(StrictCalibrationModel):
    algorithm: Literal["ordered-stream"]
    version: Literal["v1"]


class CorpusFinalizationProvenance(StrictCalibrationModel):
    finalizer: GeneratorIdentity
    integrity_reconciliation: IntegrityReconciliationIdentity


class CampaignPartitionPlanEntry(StrictCalibrationModel):
    source_partition_id: str = Field(pattern=_ID)
    raw_source: SourceReference
    raw_row_count: int = Field(ge=0)
    logical_start_date: date
    logical_end_date: date
    expected_shard_id: str = Field(pattern=_ID)
    relative_shard_manifest_path: str = Field(
        pattern=r"^shards/[a-z0-9._:-]+/shard-manifest\.json$"
    )
    state: Literal[
        "pending",
        "reusable_verified",
        "failed",
        "conflict",
        "invalid",
    ]
    state_reason: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_logical_dates(self) -> CampaignPartitionPlanEntry:
        if self.logical_end_date < self.logical_start_date:
            raise ValueError("Campaign partition end date cannot precede start date")
        return self


class CalibrationCampaignPlanV2(StrictCalibrationModel):
    plan_schema_version: Literal[1]
    artifact_type: Literal["commute_help_calibration_campaign_plan"]
    plan_id: str = Field(pattern=_ID)
    generated_at: datetime
    campaign_manifest: SourceReference
    source_dataset: SourceDatasetIdentity
    expected_partition_count: int = Field(ge=1)
    partitions: tuple[CampaignPartitionPlanEntry, ...] = Field(min_length=1)
    incomplete_temporary_output_count: int = Field(ge=0)
    plan_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_plan(self) -> CalibrationCampaignPlanV2:
        _require_aware(self.generated_at, "generated_at")
        partition_ids = [entry.source_partition_id for entry in self.partitions]
        if partition_ids != sorted(partition_ids) or len(partition_ids) != len(set(partition_ids)):
            raise ValueError("Campaign plan partitions must be uniquely sorted")
        if self.expected_partition_count != len(self.partitions):
            raise ValueError("Expected partition count must equal plan entries")
        return self


class CalibrationObservationShardV2(StrictCalibrationModel):
    corpus_schema_version: Literal[CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION]
    shard_schema_version: Literal[1]
    artifact_type: Literal["commute_help_calibration_observation_shard"]
    shard_id: str = Field(pattern=_ID)
    source_partition_id: str = Field(pattern=_ID)
    calibration_schema_version: Literal[CALIBRATION_V2_SCHEMA_VERSION]
    artifact_status: Literal["historical_input"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    generator: GeneratorIdentity
    source_dataset: SourceDatasetIdentity
    source_references: tuple[SourceReference, ...] = Field(min_length=1)
    observation_stream: ObservationStream
    audit: ImportAudit
    earliest_interval_start: datetime | None = None
    latest_interval_end: datetime | None = None
    weekdays_represented: tuple[Weekday, ...] = Field(default_factory=tuple)
    station_count: int = Field(ge=0)
    detector_count: int = Field(ge=0)
    mapping_coverage: MappingCoverage
    content_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_shard(self) -> CalibrationObservationShardV2:
        _require_aware(self.generated_at, "generated_at")
        if self.source_dataset.synthetic:
            raise ValueError("PORTAL observation shards cannot use a synthetic source dataset")
        if self.audit.accepted_count != self.observation_stream.record_count:
            raise ValueError("Accepted count must equal observation stream record count")
        if (self.earliest_interval_start is None) != (self.latest_interval_end is None):
            raise ValueError("Shard interval bounds must be provided together")
        if self.earliest_interval_start is not None:
            _require_aware(self.earliest_interval_start, "earliest_interval_start")
            _require_aware(self.latest_interval_end, "latest_interval_end")
            if self.latest_interval_end <= self.earliest_interval_start:
                raise ValueError("Shard interval bounds are invalid")
        return self


class CorpusShardIndexEntry(StrictCalibrationModel):
    shard_id: str = Field(pattern=_ID)
    relative_path: str = Field(pattern=r"^shards/[a-z0-9._:-]+/shard-manifest\.json$")
    content_digest: str = Field(pattern=_DIGEST)
    observation_stream_canonical_sha256: str = Field(pattern=_DIGEST)
    observation_stream_physical_sha256: str = Field(pattern=_DIGEST)
    accepted_count: int = Field(ge=0)
    earliest_interval_start: datetime | None = None
    latest_interval_end: datetime | None = None


class CalibrationObservationCorpusV2(StrictCalibrationModel):
    corpus_schema_version: Literal[CALIBRATION_V2_CORPUS_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_calibration_observation_corpus"]
    corpus_id: str = Field(pattern=_ID)
    calibration_schema_version: Literal[CALIBRATION_V2_SCHEMA_VERSION]
    artifact_status: Literal["historical_input"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    generator: GeneratorIdentity
    finalization: CorpusFinalizationProvenance | None = None
    source_dataset: SourceDatasetIdentity
    shards: tuple[CorpusShardIndexEntry, ...]
    observation_count: int = Field(ge=0)
    materialization_state: Literal[
        "incomplete",
        "structurally_valid",
        "duplicates_resolved_logically",
        "conflicts_present",
        "complete_unvalidated",
    ] = "incomplete"
    integrity: CorpusIntegritySummary | None = None
    corpus_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_corpus(self) -> CalibrationObservationCorpusV2:
        _require_aware(self.generated_at, "generated_at")
        if self.source_dataset.synthetic:
            raise ValueError("PORTAL observation corpus cannot use a synthetic source dataset")
        ids = [entry.shard_id for entry in self.shards]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("Corpus shards must be uniquely sorted by shard ID")
        if self.observation_count != sum(entry.accepted_count for entry in self.shards):
            raise ValueError("Corpus observation count must equal shard accepted counts")
        if self.integrity is not None:
            if self.integrity.indexed_shard_count != len(self.shards):
                raise ValueError("Corpus integrity index must cover every indexed shard")
            if self.integrity.physical_observation_count != self.observation_count:
                raise ValueError("Corpus integrity physical count must equal shard count")
            if self.materialization_state == "conflicts_present" and not self.integrity.global_conflict_identity_count:
                raise ValueError("conflicts_present requires global conflict evidence")
            if self.materialization_state == "duplicates_resolved_logically" and not self.integrity.global_duplicate_claim_count:
                raise ValueError("duplicates_resolved_logically requires duplicate evidence")
        finalized_states = {
            "complete_unvalidated",
            "duplicates_resolved_logically",
            "conflicts_present",
        }
        if self.materialization_state in finalized_states and self.finalization is None:
            raise ValueError("Finalized corpus states require finalization provenance")
        if self.finalization is not None and self.integrity is None:
            raise ValueError("Finalization provenance requires an integrity result")
        return self
