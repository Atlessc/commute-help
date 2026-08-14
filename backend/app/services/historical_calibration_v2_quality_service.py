"""Read-only, bounded characterization of Phase 1.3 candidate profiles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
from pyarrow import parquet

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_quality import (
    QUALITY_CHARACTERIZATION_SCHEMA_VERSION,
    QualityCharacterizationManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

ANALYZER_NAME = "historical_calibration_v2_quality_characterizer"
ANALYZER_VERSION = "phase-1.3-quality-v1"
PROFILE_OUTPUT = "profile-quality-characterization.parquet"
JSON_OUTPUT = "quality-characterization.json"
MARKDOWN_OUTPUT = "QUALITY_CHARACTERIZATION.md"
MANIFEST_OUTPUT = "quality-characterization-manifest.json"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKDAY_INDEX = {value: index for index, value in enumerate(WEEKDAYS)}
DATE_LEVEL_FIELDS = (
    "schema_version",
    "record_id",
    "app_edge_id",
    "graph_version",
    "direction",
    "local_date",
    "weekday",
    "bucket_start_minute",
    "interval_seconds",
    "volume_count",
    "flow_vph",
    "speed_kph",
    "occupancy_percent",
    "reference_speed_kph",
    "slowdown",
    "station_count",
    "detector_count",
    "sample_count",
    "source_station_ids_json",
    "quality_flags_json",
    "evidence_level",
    "calibration_status",
)
DATE_INPUT_COLUMNS = (
    "app_edge_id",
    "direction",
    "local_date",
    "weekday",
    "bucket_start_minute",
    "flow_vph",
    "speed_kph",
    "occupancy_percent",
    "reference_speed_kph",
    "station_count",
    "detector_count",
    "sample_count",
)
PROFILE_KEY = ("app_edge_id", "weekday", "bucket_start_minute")
PROFILE_DISTRIBUTION_COLUMNS = (
    "volume_count_mean",
    "volume_count_p50",
    "volume_count_p85",
    "volume_count_p90",
    "volume_count_p95",
    "flow_vph_mean",
    "flow_vph_p50",
    "flow_vph_p85",
    "flow_vph_p90",
    "flow_vph_p95",
    "speed_kph_mean",
    "speed_kph_p10",
    "speed_kph_p50",
    "speed_kph_p85",
    "speed_kph_p90",
    "speed_kph_p95",
    "occupancy_percent_mean",
    "occupancy_percent_p50",
    "occupancy_percent_p85",
    "occupancy_percent_p95",
    "slowdown_p50",
    "slowdown_p85",
    "slowdown_p90",
    "slowdown_p95",
)
PROFILE_SUPPORT_COLUMNS = (
    "sample_days",
    "volume_sample_days",
    "speed_sample_days",
    "occupancy_sample_days",
    "slowdown_sample_days",
)
PROFILE_LEVEL_FIELDS = (
    "schema_version",
    "record_id",
    "app_edge_id",
    "graph_version",
    "direction",
    "weekday",
    "bucket_start_minute",
    "interval_seconds",
    "source_window_start",
    "source_window_end",
    *PROFILE_SUPPORT_COLUMNS,
    *PROFILE_DISTRIBUTION_COLUMNS,
    "source_station_ids_json",
    "quality_flags_json",
    "profile_status",
    "evidence_level",
    "calibration_status",
)
COUNT_FIELDS = (
    "possible_date_count",
    "observed_date_count",
    "missing_date_count",
    "present_flow_date_count",
    "missing_flow_date_count",
    "zero_flow_date_count",
    "present_speed_date_count",
    "missing_speed_date_count",
    "zero_speed_date_count",
    "present_occupancy_date_count",
    "missing_occupancy_date_count",
    "occupancy_above_100_date_count",
    "sample_count_present_date_count",
    "sample_count_missing_date_count",
    "sample_count_zero_date_count",
    "zero_speed_positive_flow_date_count",
    "zero_speed_zero_flow_date_count",
    "zero_speed_occupancy_present_date_count",
)
FRACTION_FIELDS = (
    "coverage_fraction",
    "zero_flow_fraction_present",
    "zero_speed_fraction_present",
    "occupancy_above_100_fraction_present",
)
SUPPORT_STAT_FIELDS = tuple(
    f"{prefix}_{stat}"
    for prefix in (
        "sample_count",
        "detector_support_count",
        "station_support_count",
        "zero_speed_sample_count",
        "zero_speed_occupancy_percent",
        "reference_speed_kph",
    )
    for stat in (
        "min",
        "p01",
        "p05",
        "p10",
        "p25",
        "p50",
        "p75",
        "p90",
        "p95",
        "p99",
        "max",
    )
)
Log = Callable[[str], None]


CHARACTERIZATION_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        *[pa.field(name, pa.int32(), nullable=False) for name in COUNT_FIELDS],
        *[pa.field(name, pa.float64()) for name in FRACTION_FIELDS],
        *[pa.field(name, pa.float64()) for name in SUPPORT_STAT_FIELDS],
        *[pa.field(name, pa.float64()) for name in PROFILE_DISTRIBUTION_COLUMNS],
        pa.field("source_station_ids_json", pa.string(), nullable=False),
        pa.field("source_profile_quality_flags_json", pa.string(), nullable=False),
        pa.field("source_profile_status", pa.string(), nullable=False),
        pa.field("source_evidence_level", pa.string(), nullable=False),
        pa.field("calibration_status", pa.string(), nullable=False),
        pa.field("characterization_status", pa.string(), nullable=False),
    ]
)

DATE_PARTITION_SCHEMA = pa.schema(
    [
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("local_date", pa.date32(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("flow_vph", pa.float64(), nullable=False),
        pa.field("speed_kph", pa.float64()),
        pa.field("occupancy_percent", pa.float64()),
        pa.field("reference_speed_kph", pa.float64(), nullable=False),
        pa.field("station_count", pa.int32(), nullable=False),
        pa.field("detector_count", pa.int32(), nullable=False),
        pa.field("sample_count", pa.int64(), nullable=False),
    ]
)


class HistoricalQualityCharacterizationError(RuntimeError):
    """The Phase 1.3 artifacts cannot support a sound characterization."""


@dataclass(frozen=True)
class _ColumnLimits:
    sample_max: int
    detector_max: int
    station_max: int
    occupancy_max: float


class _DateEvidence:
    """Bounded exact counters and histograms collected during one date-table scan."""

    def __init__(self, edges: tuple[str, ...], limits: _ColumnLimits) -> None:
        self.edges = edges
        self.edge_index = {edge: index for index, edge in enumerate(edges)}
        self.counts_overall = np.zeros(9, dtype=np.uint64)
        self.counts_weekday = np.zeros((5, 9), dtype=np.uint64)
        self.counts_bucket = np.zeros((96, 9), dtype=np.uint64)
        self.counts_edge = np.zeros((len(edges), 9), dtype=np.uint64)
        self.sample_hist = np.zeros(limits.sample_max + 1, dtype=np.uint64)
        self.sample_hist_zero_speed = np.zeros_like(self.sample_hist)
        self.sample_hist_occupancy_above_100 = np.zeros_like(self.sample_hist)
        self.sample_hist_speed_missing = np.zeros_like(self.sample_hist)
        self.sample_hist_occupancy_missing = np.zeros_like(self.sample_hist)
        self.sample_hist_flow_missing = np.zeros_like(self.sample_hist)
        self.sample_hist_weekday = np.zeros((5, len(self.sample_hist)), dtype=np.uint32)
        self.sample_hist_bucket = np.zeros((96, len(self.sample_hist)), dtype=np.uint32)
        self.sample_hist_edge = np.zeros(
            (len(edges), len(self.sample_hist)), dtype=np.uint32
        )
        self.detector_hist = np.zeros(limits.detector_max + 1, dtype=np.uint64)
        self.station_hist = np.zeros(limits.station_max + 1, dtype=np.uint64)
        self.detector_hist_edge = np.zeros(
            (len(edges), len(self.detector_hist)), dtype=np.uint32
        )
        self.station_hist_edge = np.zeros(
            (len(edges), len(self.station_hist)), dtype=np.uint32
        )
        self.occupancy_bin_width = 0.125
        occupancy_bins = math.ceil(limits.occupancy_max / self.occupancy_bin_width) + 1
        self.occupancy_hist = np.zeros(occupancy_bins, dtype=np.uint64)
        self.occupancy_hist_zero_speed = np.zeros(occupancy_bins, dtype=np.uint64)
        self.occupancy_max = limits.occupancy_max
        self.dates_seen: set[date] = set()

    def add(self, frame: pd.DataFrame) -> None:
        weekday_values = frame["weekday"].map(WEEKDAY_INDEX)
        if weekday_values.isna().any():
            raise HistoricalQualityCharacterizationError(
                "Date table contains a weekend or unknown weekday"
            )
        weekday = weekday_values.to_numpy(dtype=np.int16)
        bucket = frame["bucket_start_minute"].to_numpy(dtype=np.int16) // 15
        edge_values = frame["app_edge_id"].map(self.edge_index)
        if edge_values.isna().any():
            raise HistoricalQualityCharacterizationError(
                "Date table contains an edge absent from candidate profiles"
            )
        edge = edge_values.to_numpy(dtype=np.int32)
        flow = frame["flow_vph"].to_numpy(dtype=np.float64)
        speed = frame["speed_kph"].to_numpy(dtype=np.float64)
        occupancy = frame["occupancy_percent"].to_numpy(dtype=np.float64)
        sample = frame["sample_count"].to_numpy(dtype=np.int64)
        detector = frame["detector_count"].to_numpy(dtype=np.int64)
        station = frame["station_count"].to_numpy(dtype=np.int64)
        self.dates_seen.update(frame["local_date"].tolist())
        masks = np.column_stack(
            (
                np.ones(len(frame), dtype=bool),
                np.isfinite(flow),
                np.isfinite(speed),
                np.isfinite(occupancy),
                np.isfinite(flow) & (flow == 0),
                np.isfinite(speed) & (speed == 0),
                np.isfinite(occupancy) & (occupancy > 100),
                sample == 0,
                np.isfinite(speed)
                & (speed == 0)
                & np.isfinite(occupancy)
                & (occupancy > 100),
            )
        )
        self.counts_overall += masks.sum(axis=0, dtype=np.uint64)
        self._segment_add(self.counts_weekday, weekday, masks)
        self._segment_add(self.counts_bucket, bucket, masks)
        self._segment_add(self.counts_edge, edge, masks)
        np.add.at(self.sample_hist, sample, 1)
        np.add.at(self.sample_hist_weekday, (weekday, sample), 1)
        np.add.at(self.sample_hist_bucket, (bucket, sample), 1)
        np.add.at(self.sample_hist_edge, (edge, sample), 1)
        np.add.at(self.detector_hist, detector, 1)
        np.add.at(self.station_hist, station, 1)
        np.add.at(self.detector_hist_edge, (edge, detector), 1)
        np.add.at(self.station_hist_edge, (edge, station), 1)
        occupancy_present = np.isfinite(occupancy)
        occupancy_bins = np.floor(
            occupancy[occupancy_present] / self.occupancy_bin_width
        ).astype(np.int64)
        np.add.at(self.occupancy_hist, occupancy_bins, 1)
        zero_speed = masks[:, 5]
        np.add.at(self.sample_hist_zero_speed, sample[zero_speed], 1)
        np.add.at(self.sample_hist_occupancy_above_100, sample[masks[:, 6]], 1)
        np.add.at(self.sample_hist_speed_missing, sample[~np.isfinite(speed)], 1)
        np.add.at(
            self.sample_hist_occupancy_missing,
            sample[~np.isfinite(occupancy)],
            1,
        )
        np.add.at(self.sample_hist_flow_missing, sample[~np.isfinite(flow)], 1)
        zero_speed_occupancy = zero_speed & occupancy_present
        zero_occupancy_bins = np.floor(
            occupancy[zero_speed_occupancy] / self.occupancy_bin_width
        ).astype(np.int64)
        np.add.at(self.occupancy_hist_zero_speed, zero_occupancy_bins, 1)

    @staticmethod
    def _segment_add(
        target: np.ndarray, indices: np.ndarray, masks: np.ndarray
    ) -> None:
        for column in range(masks.shape[1]):
            target[:, column] += np.bincount(
                indices, weights=masks[:, column], minlength=len(target)
            ).astype(np.uint64)


def characterize_historical_calibration_v2_quality(
    *,
    source_directory: Path,
    output_directory: Path,
    edge_partition_count: int = 48,
    batch_size: int = 131_072,
    log: Log = print,
) -> QualityCharacterizationManifestV1:
    """Characterize candidate evidence without changing or promoting its source."""
    if edge_partition_count < 1:
        raise ValueError("edge_partition_count must be positive")
    source_directory = source_directory.resolve()
    output_directory = output_directory.resolve()
    if output_directory.exists():
        raise HistoricalQualityCharacterizationError(
            "Characterization output already exists and is immutable"
        )
    source_manifest_path = source_directory / "historical-calibration-manifest.json"
    source_manifest_bytes = source_manifest_path.read_bytes()
    source_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        source_manifest_bytes
    )
    date_path = source_directory / source_manifest.date_level.relative_path
    profile_path = source_directory / source_manifest.weekday_profiles.relative_path
    _validate_input_metadata(date_path, source_manifest.date_level.model_dump())
    _validate_input_metadata(
        profile_path, source_manifest.weekday_profiles.model_dump()
    )
    date_file = parquet.ParquetFile(date_path)
    profile_file = parquet.ParquetFile(profile_path)
    _validate_schemas(date_file.schema_arrow, profile_file.schema_arrow)
    limits = _column_limits(date_file)
    profiles = profile_file.read().to_pandas()
    profiles = _validate_profiles(profiles)
    edges = tuple(sorted(profiles["app_edge_id"].astype(str).unique()))
    possible_dates = _possible_weekday_dates(
        source_manifest.source_window_start, source_manifest.source_window_end
    )
    edge_partition = _edge_partitions(edges, edge_partition_count)
    evidence = _DateEvidence(edges, limits)
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    work = staging / ".quality-work"
    work.mkdir()
    writers: dict[int, parquet.ParquetWriter] = {}
    paths: dict[int, Path] = {}
    try:
        scanned = 0
        for batch_number, batch in enumerate(
            date_file.iter_batches(
                batch_size=batch_size, columns=list(DATE_INPUT_COLUMNS)
            ),
            start=1,
        ):
            frame = batch.to_pandas()
            evidence.add(frame)
            frame["partition_id"] = frame["app_edge_id"].map(edge_partition)
            if frame["partition_id"].isna().any():
                raise HistoricalQualityCharacterizationError(
                    "Date-level edge has no candidate profile partition"
                )
            for partition_id, partition_frame in frame.groupby(
                "partition_id", sort=True
            ):
                partition = int(partition_id)
                table = pa.Table.from_pandas(
                    partition_frame[list(DATE_INPUT_COLUMNS)],
                    schema=DATE_PARTITION_SCHEMA,
                    preserve_index=False,
                )
                writer = writers.get(partition)
                if writer is None:
                    path = work / f"date-edge-part-{partition:04d}.parquet"
                    writer = parquet.ParquetWriter(
                        path, DATE_PARTITION_SCHEMA, compression="zstd"
                    )
                    writers[partition] = writer
                    paths[partition] = path
                writer.write_table(table)
            scanned += len(frame)
            if batch_number % 20 == 0:
                log(f"SCAN date rows={scanned:,}/{date_file.metadata.num_rows:,}")
        for writer in writers.values():
            writer.close()
        if scanned != source_manifest.date_level.row_count:
            raise HistoricalQualityCharacterizationError(
                "Date-level scan count does not match the source manifest"
            )

        output_path = staging / PROFILE_OUTPUT
        output_writer = parquet.ParquetWriter(
            output_path,
            CHARACTERIZATION_SCHEMA,
            compression="zstd",
            use_dictionary=True,
        )
        output_rows = 0
        try:
            for partition_id, path in sorted(paths.items()):
                date_frame = parquet.read_table(path).to_pandas()
                partition_edges = {
                    edge
                    for edge, value in edge_partition.items()
                    if value == partition_id
                }
                profile_frame = profiles[profiles["app_edge_id"].isin(partition_edges)]
                rows = _characterize_partition(
                    date_frame,
                    profile_frame,
                    possible_dates=possible_dates,
                    source_digest=source_manifest.content_digest,
                )
                output_writer.write_table(
                    pa.Table.from_pylist(rows, schema=CHARACTERIZATION_SCHEMA)
                )
                output_rows += len(rows)
                log(
                    f"PROFILES partitions={partition_id + 1:,}/{edge_partition_count:,} "
                    f"rows={output_rows:,}"
                )
        finally:
            output_writer.close()
        shutil.rmtree(work)
        if output_rows != source_manifest.weekday_profiles.row_count:
            raise HistoricalQualityCharacterizationError(
                "Characterization output does not cover every candidate profile"
            )

        characterization = parquet.read_table(output_path).to_pandas()
        report = _build_report(
            source_manifest,
            profiles,
            characterization,
            evidence,
            possible_dates,
        )
        json_path = staging / JSON_OUTPUT
        markdown_path = staging / MARKDOWN_OUTPUT
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        markdown_path.write_text(_report_markdown(report), encoding="utf-8")
        manifest_payload: dict[str, Any] = {
            "schema_version": QUALITY_CHARACTERIZATION_SCHEMA_VERSION,
            "artifact_type": "commute_help_historical_calibration_quality_characterization",
            "evidence_status": "descriptive_characterization_only",
            "calibration_status": "not_calibrated",
            "source_profile_status": "candidate_unvalidated",
            "generated_at": datetime.now(UTC),
            "analyzer": {
                "name": ANALYZER_NAME,
                "code_version": ANALYZER_VERSION,
                "model_version": None,
            },
            "methodology_version": ANALYZER_VERSION,
            "methodology": {
                "grain": "app_edge_id_weekday_15_minute_bucket",
                "possible_dates": "inclusive_source_window_calendar",
                "measurement_missingness": "possible_dates_minus_present_measurement_dates",
                "quantiles": "exact_linear_within_profile",
                "aggregation": "descriptive_only_no_eligibility_policy",
                "edge_partitioning": "contiguous_sorted_edge_ranges",
            },
            "source_phase_1_3_manifest_sha256": hashlib.sha256(
                source_manifest_bytes
            ).hexdigest(),
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_corpus_digest": source_manifest.source_corpus_digest,
            "source_integrity_digest": source_manifest.source_integrity_digest,
            "graph_version": source_manifest.graph_version,
            "input_date_level": _input_identity(
                source_manifest.date_level.model_dump()
            ),
            "input_weekday_profiles": _input_identity(
                source_manifest.weekday_profiles.model_dump()
            ),
            "output_profile_characterization": _file_identity(
                output_path, PROFILE_OUTPUT, output_rows
            ),
            "output_json": _file_identity(json_path, JSON_OUTPUT),
            "output_markdown": _file_identity(markdown_path, MARKDOWN_OUTPUT),
        }
        manifest_payload["content_digest"] = _content_digest(manifest_payload)
        manifest = QualityCharacterizationManifestV1.model_validate(manifest_payload)
        (staging / MANIFEST_OUTPUT).write_text(
            canonical_json(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        os.replace(staging, output_directory)
        return manifest
    except BaseException:
        for writer in writers.values():
            try:
                writer.close()
            except (OSError, pa.ArrowException):
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_input_metadata(path: Path, identity: dict[str, Any]) -> None:
    if not path.is_file() or path.stat().st_size != identity["byte_count"]:
        raise HistoricalQualityCharacterizationError(
            f"Input file metadata mismatch: {path.name}"
        )
    if parquet.read_metadata(path).num_rows != identity["row_count"]:
        raise HistoricalQualityCharacterizationError(
            f"Input row count mismatch: {path.name}"
        )


def _validate_schemas(date_schema: pa.Schema, profile_schema: pa.Schema) -> None:
    if tuple(date_schema.names) != DATE_LEVEL_FIELDS:
        raise HistoricalQualityCharacterizationError(
            "Date-level schema differs from the Phase 1.3-v1 contract"
        )
    if tuple(profile_schema.names) != PROFILE_LEVEL_FIELDS:
        raise HistoricalQualityCharacterizationError(
            "Profile schema differs from the Phase 1.3-v1 contract"
        )
    missing_date = sorted(set(DATE_INPUT_COLUMNS) - set(date_schema.names))
    missing_profile = sorted(
        {*PROFILE_KEY, *PROFILE_SUPPORT_COLUMNS, *PROFILE_DISTRIBUTION_COLUMNS}
        - set(profile_schema.names)
    )
    if missing_date or missing_profile:
        raise HistoricalQualityCharacterizationError(
            f"Derived schema is missing fields: date={missing_date}, profile={missing_profile}"
        )


def _column_limits(file: parquet.ParquetFile) -> _ColumnLimits:
    def maximum(name: str) -> float:
        column = file.schema_arrow.get_field_index(name)
        values = []
        for row_group in range(file.metadata.num_row_groups):
            stats = file.metadata.row_group(row_group).column(column).statistics
            if stats is not None and stats.has_min_max:
                values.append(float(stats.max))
        if not values:
            raise HistoricalQualityCharacterizationError(
                f"Parquet metadata lacks maximum for {name}"
            )
        return max(values)

    return _ColumnLimits(
        sample_max=int(maximum("sample_count")),
        detector_max=int(maximum("detector_count")),
        station_max=int(maximum("station_count")),
        occupancy_max=maximum("occupancy_percent"),
    )


def _validate_profiles(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.duplicated(list(PROFILE_KEY)).any():
        raise HistoricalQualityCharacterizationError(
            "Candidate profile identity is not unique"
        )
    if set(frame["weekday"]) != set(WEEKDAYS):
        raise HistoricalQualityCharacterizationError(
            "Candidate profiles are not Monday-Friday only"
        )
    if set(frame["profile_status"]) != {"candidate"}:
        raise HistoricalQualityCharacterizationError(
            "Source profiles are not candidate-only"
        )
    if set(frame["calibration_status"]) != {"not_calibrated"}:
        raise HistoricalQualityCharacterizationError(
            "Source profile calibration state changed"
        )
    order = frame["weekday"].map(WEEKDAY_INDEX)
    return (
        frame.assign(_weekday_order=order)
        .sort_values(
            ["app_edge_id", "_weekday_order", "bucket_start_minute"],
            kind="mergesort",
        )
        .drop(columns="_weekday_order")
        .reset_index(drop=True)
    )


def _possible_weekday_dates(start: date, end: date) -> dict[str, int]:
    counts = Counter()
    current = start
    while current <= end:
        if current.weekday() < 5:
            counts[WEEKDAYS[current.weekday()]] += 1
        current += timedelta(days=1)
    return {weekday: counts[weekday] for weekday in WEEKDAYS}


def _edge_partitions(edges: tuple[str, ...], count: int) -> dict[str, int]:
    width = math.ceil(len(edges) / count)
    return {edge: min(index // width, count - 1) for index, edge in enumerate(edges)}


def _characterize_partition(
    date_frame: pd.DataFrame,
    profile_frame: pd.DataFrame,
    *,
    possible_dates: dict[str, int],
    source_digest: str,
) -> list[dict[str, Any]]:
    if date_frame.duplicated(list(PROFILE_KEY) + ["local_date"]).any():
        raise HistoricalQualityCharacterizationError(
            "Date-level input has duplicate profile/date identities"
        )
    grouped = {
        key: values
        for key, values in date_frame.groupby(
            list(PROFILE_KEY), sort=True, observed=True
        )
    }
    profile_keys = {
        tuple(row[column] for column in PROFILE_KEY)
        for _, row in profile_frame.iterrows()
    }
    unknown = sorted(set(grouped) - profile_keys)
    if unknown:
        raise HistoricalQualityCharacterizationError(
            f"Date rows lack candidate profiles: {unknown[:5]}"
        )
    rows: list[dict[str, Any]] = []
    for _, profile in profile_frame.iterrows():
        key = tuple(profile[column] for column in PROFILE_KEY)
        values = grouped.get(key)
        if values is None or values.empty:
            raise HistoricalQualityCharacterizationError(
                f"Candidate profile lacks date evidence: {key}"
            )
        weekday = str(profile["weekday"])
        possible = possible_dates[weekday]
        observed = len(values)
        if observed != int(profile["sample_days"]) or observed > possible:
            raise HistoricalQualityCharacterizationError(
                f"Profile sample-day mismatch: {key}"
            )
        flow = _finite(values["flow_vph"])
        speed = _finite(values["speed_kph"])
        occupancy = _finite(values["occupancy_percent"])
        sample = _finite(values["sample_count"])
        detector = _finite(values["detector_count"])
        station = _finite(values["station_count"])
        reference_speed = _finite(values["reference_speed_kph"])
        zero_speed_values = values[
            pd.to_numeric(values["speed_kph"], errors="coerce").eq(0)
        ]
        zero_speed_sample = _finite(zero_speed_values["sample_count"])
        zero_speed_occupancy = _finite(zero_speed_values["occupancy_percent"])
        expected_support = {
            "sample_days": observed,
            "volume_sample_days": len(flow),
            "speed_sample_days": len(speed),
            "occupancy_sample_days": len(occupancy),
        }
        for column, expected in expected_support.items():
            if int(profile[column]) != expected:
                raise HistoricalQualityCharacterizationError(
                    f"Profile {column} disagrees with date evidence: {key}"
                )
        row: dict[str, Any] = {
            "schema_version": 1,
            "record_id": _record_id(source_digest, key),
            "app_edge_id": str(profile["app_edge_id"]),
            "direction": str(profile["direction"]),
            "weekday": weekday,
            "bucket_start_minute": int(profile["bucket_start_minute"]),
            "possible_date_count": possible,
            "observed_date_count": observed,
            "coverage_fraction": observed / possible,
            "missing_date_count": possible - observed,
            "present_flow_date_count": len(flow),
            "missing_flow_date_count": possible - len(flow),
            "zero_flow_date_count": int(flow.eq(0).sum()),
            "zero_flow_fraction_present": _fraction(int(flow.eq(0).sum()), len(flow)),
            "present_speed_date_count": len(speed),
            "missing_speed_date_count": possible - len(speed),
            "zero_speed_date_count": int(speed.eq(0).sum()),
            "zero_speed_fraction_present": _fraction(
                int(speed.eq(0).sum()), len(speed)
            ),
            "present_occupancy_date_count": len(occupancy),
            "missing_occupancy_date_count": possible - len(occupancy),
            "occupancy_above_100_date_count": int(occupancy.gt(100).sum()),
            "occupancy_above_100_fraction_present": _fraction(
                int(occupancy.gt(100).sum()), len(occupancy)
            ),
            "sample_count_present_date_count": len(sample),
            "sample_count_missing_date_count": possible - len(sample),
            "sample_count_zero_date_count": int(sample.eq(0).sum()),
            "zero_speed_positive_flow_date_count": int(
                pd.to_numeric(zero_speed_values["flow_vph"], errors="coerce")
                .gt(0)
                .sum()
            ),
            "zero_speed_zero_flow_date_count": int(
                pd.to_numeric(zero_speed_values["flow_vph"], errors="coerce")
                .eq(0)
                .sum()
            ),
            "zero_speed_occupancy_present_date_count": len(zero_speed_occupancy),
            "source_station_ids_json": str(profile["source_station_ids_json"]),
            "source_profile_quality_flags_json": str(profile["quality_flags_json"]),
            "source_profile_status": "candidate_unvalidated",
            "source_evidence_level": str(profile["evidence_level"]),
            "calibration_status": "not_calibrated",
            "characterization_status": "descriptive_only",
        }
        row.update(_support_stats("sample_count", sample))
        row.update(_support_stats("detector_support_count", detector))
        row.update(_support_stats("station_support_count", station))
        row.update(_support_stats("zero_speed_sample_count", zero_speed_sample))
        row.update(_support_stats("zero_speed_occupancy_percent", zero_speed_occupancy))
        row.update(_support_stats("reference_speed_kph", reference_speed))
        for column in PROFILE_DISTRIBUTION_COLUMNS:
            value = profile[column]
            row[column] = float(value) if pd.notna(value) else None
        rows.append(row)
    return rows


def _finite(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric[np.isfinite(numeric)]


def _support_stats(prefix: str, values: pd.Series) -> dict[str, float | None]:
    quantiles = {
        "p01": 0.01,
        "p05": 0.05,
        "p10": 0.10,
        "p25": 0.25,
        "p50": 0.50,
        "p75": 0.75,
        "p90": 0.90,
        "p95": 0.95,
        "p99": 0.99,
    }
    result = {
        f"{prefix}_min": float(values.min()) if len(values) else None,
        f"{prefix}_max": float(values.max()) if len(values) else None,
    }
    result.update(
        {
            f"{prefix}_{name}": float(values.quantile(value)) if len(values) else None
            for name, value in quantiles.items()
        }
    )
    return result


def _build_report(
    source: HistoricalCalibrationCompilerManifestV1,
    profiles: pd.DataFrame,
    characterization: pd.DataFrame,
    evidence: _DateEvidence,
    possible_dates: dict[str, int],
) -> dict[str, Any]:
    edge_count = len(evidence.edges)
    possible_identities = edge_count * 5 * 96
    total_possible_dates = sum(possible_dates.values()) * edge_count * 96
    identity_coverage = len(profiles) / possible_identities
    mapped_fraction = source.mapped_observation_count / source.source_observation_count
    distinct_station_ids = _distinct_station_ids(profiles)
    report: dict[str, Any] = {
        "schema_version": 1,
        "analyzer": {"name": ANALYZER_NAME, "code_version": ANALYZER_VERSION},
        "evidence_status": "descriptive_characterization_only",
        "calibration_status": "not_calibrated",
        "source_profile_status": "candidate_unvalidated",
        "input": {
            "source_phase_1_3_content_digest": source.content_digest,
            "source_corpus_digest": source.source_corpus_digest,
            "source_integrity_digest": source.source_integrity_digest,
            "date_level_rows": source.date_level.row_count,
            "weekday_profile_rows": source.weekday_profiles.row_count,
            "date_level_sha256": source.date_level.sha256,
            "weekday_profiles_sha256": source.weekday_profiles.sha256,
            "accepted_edge_count": edge_count,
            "source_window_start": source.source_window_start.isoformat(),
            "source_window_end": source.source_window_end.isoformat(),
            "possible_date_count_by_weekday": possible_dates,
            "observed_exact_date_count": len(evidence.dates_seen),
            "observed_earliest_date": min(evidence.dates_seen).isoformat(),
            "observed_latest_date": max(evidence.dates_seen).isoformat(),
            "observed_weekdays": list(WEEKDAYS),
            "observed_bucket_count": int(
                np.count_nonzero(evidence.counts_bucket[:, 0])
            ),
            "represented_station_identity_count": len(distinct_station_ids),
            "represented_detector_identity_count": "NOT CURRENTLY DERIVABLE",
        },
        "schema_inventory": {
            "edge_day_15m_fields": list(DATE_LEVEL_FIELDS),
            "edge_time_distributions_fields": list(PROFILE_LEVEL_FIELDS),
            "field_groups": {
                "identity_calendar": [
                    "app_edge_id",
                    "graph_version",
                    "direction",
                    "local_date",
                    "weekday",
                    "bucket_start_minute",
                    "interval_seconds",
                    "source_window_start",
                    "source_window_end",
                ],
                "date_measurements": [
                    "volume_count",
                    "flow_vph",
                    "speed_kph",
                    "occupancy_percent",
                    "reference_speed_kph",
                    "slowdown",
                ],
                "date_source_support": [
                    "sample_count",
                    "detector_count",
                    "station_count",
                    "source_station_ids_json",
                ],
                "profile_support": list(PROFILE_SUPPORT_COLUMNS),
                "profile_distributions": list(PROFILE_DISTRIBUTION_COLUMNS),
                "quality_and_status": [
                    "quality_flags_json",
                    "profile_status",
                    "evidence_level",
                    "calibration_status",
                ],
            },
            "quality_evidence": {
                "sample_count": "aggregated edge-date-bucket countreadings evidence",
                "detector_count": "number of contributing detector observations",
                "station_count": "number of contributing accepted stations",
                "missingness": "nullable speed/occupancy/slowdown plus absent dates",
                "zero_speed": "directly derivable from date-level speed_kph",
                "occupancy_above_100": "directly derivable from date-level occupancy_percent",
                "reference_speed": "date-level reference_speed_kph",
            },
        },
        "profile_identity_coverage": {
            "possible_identities": possible_identities,
            "actual_candidate_identities": len(profiles),
            "identity_coverage_fraction": identity_coverage,
            "identity_coverage_percent": identity_coverage * 100,
            "theoretical_dense_edge_date_bucket_rows": total_possible_dates,
            "actual_date_level_rows": source.date_level.row_count,
            "dense_date_row_fraction": source.date_level.row_count
            / total_possible_dates,
        },
        "date_support": _date_support(characterization),
        "missingness": _missingness(characterization, evidence),
        "zero_speed": _zero_speed(characterization, evidence),
        "occupancy_above_100": _occupancy_above_100(characterization, evidence),
        "sample_count": _sample_count(evidence),
        "edge_support": _edge_support(characterization, evidence, possible_dates),
        "time_of_day_support": _time_support(characterization, evidence),
        "unmapped_observations": {
            "mapped_observations": source.mapped_observation_count,
            "unmapped_or_review_observations": source.unmapped_observation_count,
            "mapped_fraction": mapped_fraction,
            "mapped_percent": mapped_fraction * 100,
            "unmapped_or_review_fraction": 1 - mapped_fraction,
            "unmapped_or_review_percent": (1 - mapped_fraction) * 100,
            "review_vs_unmatched_observation_split": "NOT CURRENTLY DERIVABLE",
        },
        "derivability": {
            "available": [
                "exact edge/date/weekday/bucket identity",
                "flow, speed, occupancy, slowdown profile distributions",
                "date-level sample_count, detector_count, and station_count",
                "zero-speed and occupancy-above-100 frequencies",
                "reference speed and compiler quality flags",
                "unique station identities through source_station_ids_json",
            ],
            "not_currently_derivable": [
                "review versus unmatched observation totals; Phase 1.2 combines them",
                "raw per-detector countreadings distributions after edge aggregation",
                "unique detector identities; only detector support counts survive",
                "provider-authenticated detector quality status; the source has none",
                "whether zero speed is congestion or detector failure",
                "physical meaning of occupancy above 100",
                "profile eligibility without a separately chosen quality policy",
            ],
        },
        "methodology": {
            "possible_dates": "inclusive source-window calendar by weekday",
            "missing_measurement_dates": "possible dates minus present measurement dates",
            "profile_quantiles": "reused from edge-time-distributions.parquet",
            "support_quantiles": "exact pandas linear quantiles within each candidate profile",
            "global_sample_quantiles": "exact integer histogram",
            "occupancy_quantiles": f"deterministic {evidence.occupancy_bin_width:g}-point histogram approximation",
            "eligibility_policy": "none",
        },
    }
    return report


def _date_support(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "observed_date_count_distribution": _series_summary(
            frame["observed_date_count"]
        ),
        "coverage_fraction_distribution": _series_summary(frame["coverage_fraction"]),
        "observed_date_count_bands": _count_bands(frame["observed_date_count"]),
        "coverage_fraction_bands": _coverage_bands(frame["coverage_fraction"]),
        "by_weekday": _group_summaries(frame, "weekday"),
        "by_time_bucket": _group_summaries(frame, "bucket_start_minute"),
    }


def _missingness(frame: pd.DataFrame, evidence: _DateEvidence) -> dict[str, Any]:
    total_rows = int(evidence.counts_overall[0])
    names = {"flow": 1, "speed": 2, "occupancy": 3, "sample_count": None}
    date_level = {}
    profile_level = {}
    for name, present_index in names.items():
        present = (
            total_rows
            if present_index is None
            else int(evidence.counts_overall[present_index])
        )
        date_level[name] = {
            "present_date_rows": present,
            "null_date_rows": total_rows - present,
            "null_fraction_of_date_rows": _fraction(total_rows - present, total_rows),
        }
        missing_column = (
            f"missing_{name}_date_count"
            if name != "sample_count"
            else "sample_count_missing_date_count"
        )
        possible_column = "possible_date_count"
        missing_fraction = frame[missing_column] / frame[possible_column]
        profile_level[name] = {
            "no_missing_dates": int(missing_fraction.eq(0).sum()),
            "some_missing_dates": int(missing_fraction.gt(0).sum()),
            "more_than_10_percent_missing": int(missing_fraction.gt(0.10).sum()),
            "more_than_25_percent_missing": int(missing_fraction.gt(0.25).sum()),
            "more_than_50_percent_missing": int(missing_fraction.gt(0.50).sum()),
        }
    return {
        "date_level": date_level,
        "profile_level": profile_level,
        "within_existing_date_rows_by_weekday": _segment_evidence(
            evidence.counts_weekday, WEEKDAYS
        ),
        "within_existing_date_rows_by_time_bucket": _segment_evidence(
            evidence.counts_bucket, tuple(range(0, 1440, 15))
        ),
        "calendar_aware_by_weekday": _measurement_coverage_groups(frame, "weekday"),
        "calendar_aware_by_time_bucket": _measurement_coverage_groups(
            frame, "bucket_start_minute"
        ),
    }


def _zero_speed(frame: pd.DataFrame, evidence: _DateEvidence) -> dict[str, Any]:
    present = int(evidence.counts_overall[2])
    zero = int(evidence.counts_overall[5])
    profile_zero = frame["zero_speed_date_count"].gt(0)
    return {
        "exact_date_zero_speed_rows": zero,
        "fraction_of_present_speed": _fraction(zero, present),
        "profiles_with_zero_speed": int(profile_zero.sum()),
        "profile_zero_speed_fraction_distribution": _series_summary(
            frame["zero_speed_fraction_present"]
        ),
        "affected_profile_zero_speed_fraction_distribution": _series_summary(
            frame.loc[profile_zero, "zero_speed_fraction_present"]
        ),
        "zero_speed_with_positive_flow": int(
            frame["zero_speed_positive_flow_date_count"].sum()
        ),
        "zero_speed_with_zero_flow": int(
            frame["zero_speed_zero_flow_date_count"].sum()
        ),
        "zero_speed_with_occupancy_above_100": int(evidence.counts_overall[8]),
        "zero_speed_sample_count_distribution": _hist_summary(
            evidence.sample_hist_zero_speed
        ),
        "zero_speed_occupancy_distribution": _hist_summary(
            evidence.occupancy_hist_zero_speed,
            width=evidence.occupancy_bin_width,
        ),
        "by_weekday": _segment_anomaly(evidence.counts_weekday, WEEKDAYS, 5, 2),
        "by_time_bucket": _segment_anomaly(
            evidence.counts_bucket, tuple(range(0, 1440, 15)), 5, 2
        ),
        "edges_highest_frequency": _edge_anomaly_ranking(evidence, 5, 2),
    }


def _occupancy_above_100(
    frame: pd.DataFrame, evidence: _DateEvidence
) -> dict[str, Any]:
    present = int(evidence.counts_overall[3])
    above = int(evidence.counts_overall[6])
    profile_above = frame["occupancy_above_100_date_count"].gt(0)
    return {
        "exact_date_rows_above_100": above,
        "fraction_of_present_occupancy": _fraction(above, present),
        "profiles_with_any_above_100": int(profile_above.sum()),
        "profile_above_100_fraction_distribution": _series_summary(
            frame["occupancy_above_100_fraction_present"]
        ),
        "affected_profile_above_100_fraction_distribution": _series_summary(
            frame.loc[profile_above, "occupancy_above_100_fraction_present"]
        ),
        "maximum_observed_occupancy": evidence.occupancy_max,
        "upper_quantiles_approximate": _hist_summary(
            evidence.occupancy_hist,
            width=evidence.occupancy_bin_width,
            quantiles=(0.90, 0.95, 0.99, 0.999, 0.9999),
        ),
        "by_weekday": _segment_anomaly(evidence.counts_weekday, WEEKDAYS, 6, 3),
        "by_time_bucket": _segment_anomaly(
            evidence.counts_bucket, tuple(range(0, 1440, 15)), 6, 3
        ),
        "edges_highest_frequency": _edge_anomaly_ranking(evidence, 6, 3),
    }


def _sample_count(evidence: _DateEvidence) -> dict[str, Any]:
    total = int(evidence.sample_hist.sum())
    return {
        "present_date_rows": total,
        "missing_date_rows_within_date_level_artifact": 0,
        "missing_fraction_within_date_level_artifact": 0.0,
        "zero_date_rows": int(evidence.sample_hist[0]),
        "zero_fraction": _fraction(int(evidence.sample_hist[0]), total),
        "distribution": _hist_summary(evidence.sample_hist),
        "natural_value_counts_top_25": _top_hist_values(evidence.sample_hist, 25),
        "bands": _sample_bands(evidence.sample_hist),
        "by_weekday": _hist_segment_summaries(evidence.sample_hist_weekday, WEEKDAYS),
        "by_time_bucket": _hist_segment_summaries(
            evidence.sample_hist_bucket, tuple(range(0, 1440, 15))
        ),
        "by_edge": _hist_segment_summaries(evidence.sample_hist_edge, evidence.edges),
        "when_speed_is_zero": _hist_summary(evidence.sample_hist_zero_speed),
        "when_occupancy_is_above_100": _hist_summary(
            evidence.sample_hist_occupancy_above_100
        ),
        "when_speed_is_missing": _hist_summary(evidence.sample_hist_speed_missing),
        "when_occupancy_is_missing": _hist_summary(
            evidence.sample_hist_occupancy_missing
        ),
        "when_flow_is_missing": _hist_summary(evidence.sample_hist_flow_missing),
    }


def _edge_support(
    frame: pd.DataFrame,
    evidence: _DateEvidence,
    possible_dates: dict[str, int],
) -> dict[str, Any]:
    theoretical_date_rows_per_edge = sum(possible_dates.values()) * 96
    edge = frame.groupby("app_edge_id", sort=True).agg(
        profile_count=("record_id", "size"),
        observed_date_rows=("observed_date_count", "sum"),
        candidate_possible_date_rows=("possible_date_count", "sum"),
        missing_flow_dates=("missing_flow_date_count", "sum"),
        missing_speed_dates=("missing_speed_date_count", "sum"),
        missing_occupancy_dates=("missing_occupancy_date_count", "sum"),
        missing_sample_count_dates=("sample_count_missing_date_count", "sum"),
        zero_flow_dates=("zero_flow_date_count", "sum"),
        zero_speed_dates=("zero_speed_date_count", "sum"),
        occupancy_above_100_dates=("occupancy_above_100_date_count", "sum"),
        sample_count_zero_dates=("sample_count_zero_date_count", "sum"),
        weekdays=("weekday", "nunique"),
        buckets=("bucket_start_minute", "nunique"),
    )
    edge["theoretical_dense_date_rows"] = theoretical_date_rows_per_edge
    edge["within_candidate_date_coverage_fraction"] = (
        edge["observed_date_rows"] / edge["candidate_possible_date_rows"]
    )
    edge["calendar_coverage_fraction"] = (
        edge["observed_date_rows"] / theoretical_date_rows_per_edge
    )
    edge["profile_identity_fraction"] = edge["profile_count"] / (5 * 96)
    edge["flow_missing_fraction"] = (
        edge["missing_flow_dates"] / edge["candidate_possible_date_rows"]
    )
    edge["speed_missing_fraction"] = (
        edge["missing_speed_dates"] / edge["candidate_possible_date_rows"]
    )
    edge["occupancy_missing_fraction"] = (
        edge["missing_occupancy_dates"] / edge["candidate_possible_date_rows"]
    )
    edge["sample_count_missing_fraction"] = (
        edge["missing_sample_count_dates"] / edge["candidate_possible_date_rows"]
    )
    edge["zero_flow_fraction"] = edge["zero_flow_dates"] / edge["observed_date_rows"]
    edge["zero_speed_fraction"] = edge["zero_speed_dates"] / edge["observed_date_rows"]
    edge["occupancy_above_100_fraction"] = (
        edge["occupancy_above_100_dates"] / edge["observed_date_rows"]
    )
    edge = edge.reset_index()
    edge_indexes = {edge_id: index for index, edge_id in enumerate(evidence.edges)}
    detailed: list[dict[str, Any]] = []
    for row in _records(edge):
        index = edge_indexes[str(row["app_edge_id"])]
        row["sample_count_distribution"] = _hist_summary(
            evidence.sample_hist_edge[index]
        )
        row["detector_support_count_distribution"] = _hist_summary(
            evidence.detector_hist_edge[index]
        )
        row["station_support_count_distribution"] = _hist_summary(
            evidence.station_hist_edge[index]
        )
        detailed.append(row)
    columns = [
        "app_edge_id",
        "profile_count",
        "observed_date_rows",
        "candidate_possible_date_rows",
        "theoretical_dense_date_rows",
        "calendar_coverage_fraction",
        "within_candidate_date_coverage_fraction",
        "profile_identity_fraction",
        "weekdays",
        "buckets",
        "flow_missing_fraction",
        "speed_missing_fraction",
        "occupancy_missing_fraction",
        "sample_count_missing_fraction",
        "zero_flow_fraction",
        "zero_speed_fraction",
        "occupancy_above_100_fraction",
    ]
    return {
        "represented_edge_count": len(edge),
        "calendar_coverage_distribution": _series_summary(
            edge["calendar_coverage_fraction"]
        ),
        "within_candidate_date_coverage_distribution": _series_summary(
            edge["within_candidate_date_coverage_fraction"]
        ),
        "lowest_calendar_coverage": _rank(
            edge, "calendar_coverage_fraction", True, columns
        ),
        "highest_calendar_coverage": _rank(
            edge, "calendar_coverage_fraction", False, columns
        ),
        "most_missing_speed": _rank(edge, "speed_missing_fraction", False, columns),
        "most_zero_speed": _rank(edge, "zero_speed_fraction", False, columns),
        "most_occupancy_above_100": _rank(
            edge, "occupancy_above_100_fraction", False, columns
        ),
        "weakest_sample_count_support": sorted(
            (
                {
                    "app_edge_id": row["app_edge_id"],
                    "sample_count_distribution": row["sample_count_distribution"],
                }
                for row in detailed
            ),
            key=lambda row: (
                float(row["sample_count_distribution"].get("p50", math.inf)),
                row["app_edge_id"],
            ),
        )[:15],
        "all_edges": detailed,
        "possible_dates_by_weekday": possible_dates,
    }


def _time_support(frame: pd.DataFrame, evidence: _DateEvidence) -> list[dict[str, Any]]:
    grouped = frame.groupby("bucket_start_minute", sort=True).agg(
        profile_count=("record_id", "size"),
        observed_date_count=("observed_date_count", "sum"),
        possible_date_count=("possible_date_count", "sum"),
        present_flow_date_count=("present_flow_date_count", "sum"),
        present_speed_date_count=("present_speed_date_count", "sum"),
        present_occupancy_date_count=("present_occupancy_date_count", "sum"),
        zero_speed_date_count=("zero_speed_date_count", "sum"),
        occupancy_above_100_date_count=("occupancy_above_100_date_count", "sum"),
    )
    grouped["coverage_fraction"] = (
        grouped["observed_date_count"] / grouped["possible_date_count"]
    )
    for measurement in ("flow", "speed", "occupancy"):
        grouped[f"{measurement}_availability_fraction"] = (
            grouped[f"present_{measurement}_date_count"]
            / grouped["possible_date_count"]
        )
    grouped["zero_speed_fraction_of_present_speed"] = (
        grouped["zero_speed_date_count"] / grouped["present_speed_date_count"]
    )
    grouped["occupancy_above_100_fraction_of_present_occupancy"] = (
        grouped["occupancy_above_100_date_count"]
        / grouped["present_occupancy_date_count"]
    )
    records = _records(grouped.reset_index())
    for bucket_index, row in enumerate(records):
        row["sample_count_distribution"] = _hist_summary(
            evidence.sample_hist_bucket[bucket_index]
        )
    return records


def _series_summary(values: pd.Series) -> dict[str, float | int | None]:
    numeric = _finite(values)
    if not len(numeric):
        return {
            name: None
            for name in (
                "count",
                "min",
                "p01",
                "p05",
                "p10",
                "p25",
                "median",
                "p75",
                "p90",
                "p95",
                "p99",
                "max",
            )
        }
    quantiles = numeric.quantile([0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99])
    return {
        "count": len(numeric),
        "min": float(numeric.min()),
        "p01": float(quantiles.loc[0.01]),
        "p05": float(quantiles.loc[0.05]),
        "p10": float(quantiles.loc[0.10]),
        "p25": float(quantiles.loc[0.25]),
        "median": float(quantiles.loc[0.50]),
        "p75": float(quantiles.loc[0.75]),
        "p90": float(quantiles.loc[0.90]),
        "p95": float(quantiles.loc[0.95]),
        "p99": float(quantiles.loc[0.99]),
        "max": float(numeric.max()),
    }


def _count_bands(values: pd.Series) -> list[dict[str, Any]]:
    bands = (
        (0, 0, "0"),
        (1, 4, "1-4"),
        (5, 9, "5-9"),
        (10, 19, "10-19"),
        (20, 39, "20-39"),
        (40, 59, "40-59"),
        (60, 79, "60-79"),
        (80, 99, "80-99"),
        (100, math.inf, "100+"),
    )
    return [
        {"band": label, "profile_count": int(values.between(low, high).sum())}
        for low, high, label in bands
    ]


def _coverage_bands(values: pd.Series) -> list[dict[str, Any]]:
    bands = (
        (0, 0.25, "0-24.99%"),
        (0.25, 0.50, "25-49.99%"),
        (0.50, 0.75, "50-74.99%"),
        (0.75, 0.90, "75-89.99%"),
        (0.90, 0.95, "90-94.99%"),
        (0.95, 1.0, "95-99.99%"),
    )
    rows = [
        {"band": label, "profile_count": int(((values >= low) & (values < high)).sum())}
        for low, high, label in bands
    ]
    rows.append({"band": "100%", "profile_count": int(values.eq(1).sum())})
    return rows


def _group_summaries(frame: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    rows = []
    for value, group in frame.groupby(column, sort=True):
        rows.append(
            {
                column: _native(value),
                "profile_count": len(group),
                "observed_date_count": _series_summary(group["observed_date_count"]),
                "coverage_fraction": _series_summary(group["coverage_fraction"]),
            }
        )
    return rows


def _measurement_coverage_groups(
    frame: pd.DataFrame, column: str
) -> list[dict[str, Any]]:
    grouped = frame.groupby(column, sort=True).agg(
        possible_date_count=("possible_date_count", "sum"),
        present_flow_date_count=("present_flow_date_count", "sum"),
        present_speed_date_count=("present_speed_date_count", "sum"),
        present_occupancy_date_count=("present_occupancy_date_count", "sum"),
        sample_count_present_date_count=("sample_count_present_date_count", "sum"),
    )
    for measurement in ("flow", "speed", "occupancy", "sample_count"):
        present_column = (
            "sample_count_present_date_count"
            if measurement == "sample_count"
            else f"present_{measurement}_date_count"
        )
        grouped[f"{measurement}_availability_fraction"] = (
            grouped[present_column] / grouped["possible_date_count"]
        )
    return _records(grouped.reset_index())


def _segment_evidence(
    matrix: np.ndarray, labels: tuple[Any, ...]
) -> list[dict[str, Any]]:
    rows = []
    for index, label in enumerate(labels):
        total, flow, speed, occupancy = (int(value) for value in matrix[index, :4])
        rows.append(
            {
                "segment": _native(label),
                "date_rows": total,
                "flow_null_rows": total - flow,
                "speed_null_rows": total - speed,
                "occupancy_null_rows": total - occupancy,
                "flow_null_fraction": _fraction(total - flow, total),
                "speed_null_fraction": _fraction(total - speed, total),
                "occupancy_null_fraction": _fraction(total - occupancy, total),
            }
        )
    return rows


def _segment_anomaly(
    matrix: np.ndarray, labels: tuple[Any, ...], numerator: int, denominator: int
) -> list[dict[str, Any]]:
    return [
        {
            "segment": _native(label),
            "count": int(matrix[index, numerator]),
            "fraction": _fraction(
                int(matrix[index, numerator]), int(matrix[index, denominator])
            ),
        }
        for index, label in enumerate(labels)
    ]


def _edge_anomaly_ranking(
    evidence: _DateEvidence, numerator: int, denominator: int
) -> list[dict[str, Any]]:
    rows = [
        {
            "app_edge_id": edge,
            "count": int(evidence.counts_edge[index, numerator]),
            "fraction": _fraction(
                int(evidence.counts_edge[index, numerator]),
                int(evidence.counts_edge[index, denominator]),
            ),
        }
        for index, edge in enumerate(evidence.edges)
    ]
    return sorted(
        rows, key=lambda row: (-float(row["fraction"] or 0), row["app_edge_id"])
    )[:15]


def _hist_summary(
    histogram: np.ndarray,
    *,
    width: float = 1.0,
    quantiles: tuple[float, ...] = (
        0.01,
        0.05,
        0.10,
        0.25,
        0.50,
        0.75,
        0.90,
        0.95,
        0.99,
    ),
) -> dict[str, Any]:
    total = int(histogram.sum())
    if not total:
        return {"count": 0}
    indexes = np.flatnonzero(histogram)
    cumulative = np.cumsum(histogram, dtype=np.uint64)
    result: dict[str, Any] = {
        "count": total,
        "min": float(indexes[0] * width),
        "max": float(indexes[-1] * width),
    }
    for quantile in quantiles:
        target = max(1, math.ceil(total * quantile))
        index = int(np.searchsorted(cumulative, target, side="left"))
        result[f"p{quantile * 100:g}"] = float(index * width)
    return result


def _top_hist_values(histogram: np.ndarray, count: int) -> list[dict[str, int]]:
    indexes = np.flatnonzero(histogram)
    rows = [{"value": int(index), "count": int(histogram[index])} for index in indexes]
    return sorted(rows, key=lambda row: (-row["count"], row["value"]))[:count]


def _sample_bands(histogram: np.ndarray) -> list[dict[str, Any]]:
    bands = (
        (0, 0, "0"),
        (1, 9, "1-9"),
        (10, 24, "10-24"),
        (25, 44, "25-44"),
        (45, 89, "45-89"),
        (90, 179, "90-179"),
        (180, 359, "180-359"),
        (360, 719, "360-719"),
        (720, math.inf, "720+"),
    )
    return [
        {
            "band": label,
            "date_row_count": int(
                histogram[low : None if high is math.inf else int(high) + 1].sum()
            ),
        }
        for low, high, label in bands
    ]


def _hist_segment_summaries(
    histograms: np.ndarray, labels: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [
        {"segment": _native(label), "distribution": _hist_summary(histograms[index])}
        for index, label in enumerate(labels)
    ]


def _rank(
    frame: pd.DataFrame,
    sort_column: str,
    ascending: bool,
    columns: list[str],
    limit: int = 15,
) -> list[dict[str, Any]]:
    ordered = frame.sort_values(
        [sort_column, "app_edge_id"], ascending=[ascending, True], kind="mergesort"
    )
    return _records(ordered[columns].head(limit))


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {key: _native(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _distinct_station_ids(profiles: pd.DataFrame) -> tuple[str, ...]:
    station_ids: set[str] = set()
    for serialized in profiles["source_station_ids_json"].unique():
        values = json.loads(str(serialized))
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise HistoricalQualityCharacterizationError(
                "source_station_ids_json is not a string list"
            )
        station_ids.update(values)
    return tuple(sorted(station_ids))


def _native(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    return value


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _input_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "relative_path": value["relative_path"],
        "sha256": value["sha256"],
        "byte_count": value["byte_count"],
        "row_count": value["row_count"],
    }


def _file_identity(
    path: Path, relative_path: str, row_count: int | None = None
) -> dict[str, Any]:
    return {
        "relative_path": relative_path,
        "sha256": _sha256_file(path),
        "byte_count": path.stat().st_size,
        "row_count": row_count,
    }


def _record_id(source_digest: str, key: tuple[Any, ...]) -> str:
    value = "\x1f".join((source_digest, *(str(part) for part in key)))
    return f"portal.profile-quality.{hashlib.sha256(value.encode()).hexdigest()[:40]}"


def _content_digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("generated_at", None)
    payload.pop("content_digest", None)
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_markdown(report: dict[str, Any]) -> str:
    identity = report["profile_identity_coverage"]
    date_support = report["date_support"]
    missing = report["missingness"]["date_level"]
    zero = report["zero_speed"]
    occupancy = report["occupancy_above_100"]
    sample = report["sample_count"]
    unmapped = report["unmapped_observations"]
    edge = report["edge_support"]
    time_support = report["time_of_day_support"]
    coverage_low = sorted(
        time_support,
        key=lambda row: (row["coverage_fraction"], row["bucket_start_minute"]),
    )[:10]
    zero_high = sorted(
        time_support,
        key=lambda row: (
            row["zero_speed_fraction_of_present_speed"] is None,
            -(row["zero_speed_fraction_of_present_speed"] or 0),
            row["bucket_start_minute"],
        ),
    )[:10]
    lines = [
        "# Phase 1.3 candidate quality characterization",
        "",
        "**Evidence status:** descriptive characterization only  ",
        "**Calibration status:** not calibrated  ",
        "**Source profile status:** candidate unvalidated",
        "",
        "## Identity versus date coverage",
        "",
        f"- Candidate identities: **{identity['actual_candidate_identities']:,} / {identity['possible_identities']:,} ({identity['identity_coverage_percent']:.3f}%)**",
        f"- Date rows: **{identity['actual_date_level_rows']:,} / {identity['theoretical_dense_edge_date_bucket_rows']:,} ({identity['dense_date_row_fraction'] * 100:.3f}%)**",
        f"- Median observed dates/profile: **{date_support['observed_date_count_distribution']['median']:.1f}**",
        f"- Median within-profile calendar coverage: **{date_support['coverage_fraction_distribution']['median'] * 100:.2f}%**",
        "",
        "Profile identity existence is not within-profile historical-date completeness.",
        "",
        "## Within-profile date support",
        "",
        "| Metric | Min | p01 | p05 | p10 | p25 | Median | p75 | p90 | p95 | p99 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        _distribution_markdown_row(
            "Observed dates", date_support["observed_date_count_distribution"]
        ),
        _distribution_markdown_row(
            "Coverage fraction", date_support["coverage_fraction_distribution"]
        ),
        "",
        "### Descriptive support bands",
        "",
        "| Observed-date band | Profile count |",
        "|---|---:|",
        *[
            f"| {row['band']} | {row['profile_count']:,} |"
            for row in date_support["observed_date_count_bands"]
        ],
        "",
        "| Coverage band | Profile count |",
        "|---|---:|",
        *[
            f"| {row['band']} | {row['profile_count']:,} |"
            for row in date_support["coverage_fraction_bands"]
        ],
        "",
        "## Date-level measurement evidence",
        "",
        "| Metric | Present rows | Null rows | Null fraction |",
        "|---|---:|---:|---:|",
    ]
    for metric in ("flow", "speed", "occupancy", "sample_count"):
        row = missing[metric]
        lines.append(
            f"| {metric} | {row['present_date_rows']:,} | {row['null_date_rows']:,} | {row['null_fraction_of_date_rows'] * 100:.4f}% |"
        )
    lines.extend(
        [
            "",
            "## Preserved anomaly evidence",
            "",
            f"- Zero-speed rows: **{zero['exact_date_zero_speed_rows']:,}** ({zero['fraction_of_present_speed'] * 100:.4f}% of present speeds)",
            f"- Profiles containing zero speed: **{zero['profiles_with_zero_speed']:,}**",
            f"- Occupancy >100 rows: **{occupancy['exact_date_rows_above_100']:,}** ({occupancy['fraction_of_present_occupancy'] * 100:.4f}% of present occupancy)",
            f"- Profiles containing occupancy >100: **{occupancy['profiles_with_any_above_100']:,}**",
            f"- Maximum preserved occupancy: **{occupancy['maximum_observed_occupancy']:.3f}**",
            f"- Sample-count rows: **{sample['present_date_rows']:,}**, zero: **{sample['zero_date_rows']:,}**",
            "",
            "### Sample-count distribution",
            "",
            _distribution_markdown_row("sample_count", sample["distribution"]),
            "",
            "## Time-of-day diagnostics",
            "",
            "Lowest calendar-coverage buckets:",
            "",
            "| Bucket | Profiles | Coverage | Speed availability |",
            "|---:|---:|---:|---:|",
            *[
                f"| {_minute_label(row['bucket_start_minute'])} | {row['profile_count']:,} | {row['coverage_fraction'] * 100:.3f}% | {row['speed_availability_fraction'] * 100:.3f}% |"
                for row in coverage_low
            ],
            "",
            "Highest zero-speed-frequency buckets:",
            "",
            "| Bucket | Zero-speed rows | Fraction of present speed |",
            "|---:|---:|---:|",
            *[
                f"| {_minute_label(row['bucket_start_minute'])} | {row['zero_speed_date_count']:,} | {row['zero_speed_fraction_of_present_speed'] * 100:.4f}% |"
                for row in zero_high
            ],
            "",
            "## Edge diagnostics",
            "",
            f"- Represented accepted edges: **{edge['represented_edge_count']:,}**",
            "- Lowest calendar coverage: "
            + ", ".join(
                f"{row['app_edge_id']} ({row['calendar_coverage_fraction'] * 100:.2f}%)"
                for row in edge["lowest_calendar_coverage"][:10]
            ),
            "- Highest calendar coverage: "
            + ", ".join(
                f"{row['app_edge_id']} ({row['calendar_coverage_fraction'] * 100:.2f}%)"
                for row in edge["highest_calendar_coverage"][:10]
            ),
            "",
            "## Mapping coverage",
            "",
            f"- Mapped observations: **{unmapped['mapped_observations']:,} ({unmapped['mapped_percent']:.3f}%)**",
            f"- Review/unmapped observations: **{unmapped['unmapped_or_review_observations']:,} ({unmapped['unmapped_or_review_percent']:.3f}%)**",
            "- Review versus unmatched observation counts are not separately derivable from the Phase 1.3 artifacts.",
            "",
            "## Policy boundary",
            "",
            "No sample-day, coverage, sample-count, outlier, zero-speed, occupancy-repair, or promotion threshold was selected. Rankings in the JSON artifact are diagnostics, not rejection lists.",
            "",
            "## Not currently derivable",
            "",
            *[
                f"- {item}"
                for item in report["derivability"]["not_currently_derivable"]
            ],
            "",
        ]
    )
    return "\n".join(lines)


def _distribution_markdown_row(label: str, distribution: dict[str, Any]) -> str:
    values = (
        distribution.get("min"),
        distribution.get("p01", distribution.get("p1")),
        distribution.get("p05", distribution.get("p5")),
        distribution.get("p10"),
        distribution.get("p25"),
        distribution.get("median", distribution.get("p50")),
        distribution.get("p75"),
        distribution.get("p90"),
        distribution.get("p95"),
        distribution.get("p99"),
        distribution.get("max"),
    )
    rendered = ["—" if value is None else f"{float(value):.4g}" for value in values]
    return f"| {label} | " + " | ".join(rendered) + " |"


def _minute_label(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"
