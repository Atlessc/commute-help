"""Bounded Phase 1.3 compiler from frozen calibration-v2 observations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.json as pajson
from pyarrow import parquet

from backend.app.schemas.calibration_v2_corpus import (
    CalibrationObservationCorpusV2,
    CalibrationObservationShardV2,
)
from backend.app.schemas.historical_calibration_v2 import (
    HISTORICAL_CALIBRATION_COMPILER_SCHEMA_VERSION,
    HistoricalAggregationPolicy,
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

COMPILER_NAME = "historical_calibration_v2_compiler"
COMPILER_CODE_VERSION = "phase-1.3-v1"
DATE_LEVEL_FILENAME = "edge-day-15m.parquet"
PROFILE_FILENAME = "edge-time-distributions.parquet"
MANIFEST_FILENAME = "historical-calibration-manifest.json"
REPORT_JSON_FILENAME = "validation-report.json"
REPORT_MARKDOWN_FILENAME = "validation-report.md"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAYS)}
Log = Callable[[str], None]

AGGREGATION_POLICY = HistoricalAggregationPolicy(
    detector_to_station_flow="sum_detector_counts",
    detector_to_station_speed="positive_volume_weighted_else_median",
    detector_to_station_occupancy="arithmetic_mean_present",
    station_to_edge="median_across_stations",
    missing_measurements="retain_null_no_imputation",
    outliers="preserve_source_values_no_threshold",
    weekday_aggregation="exact_date_equal_weight",
    quantiles="exact_partition_bounded",
    profile_promotion="candidate_unvalidated",
)

OBSERVATION_JSON_SCHEMA = pa.schema(
    [
        pa.field("direction", pa.string()),
        pa.field(
            "detector",
            pa.struct(
                [
                    pa.field("station_id", pa.string()),
                    pa.field("detector_id", pa.string()),
                ]
            ),
        ),
        pa.field(
            "calendar",
            pa.struct(
                [
                    pa.field("exact_date", pa.string()),
                    pa.field("weekday", pa.string()),
                    pa.field("interval_start", pa.string()),
                ]
            ),
        ),
        pa.field(
            "road_association",
            pa.struct(
                [
                    pa.field("graph_version", pa.string()),
                    pa.field("app_edge_id", pa.string()),
                ]
            ),
        ),
        pa.field(
            "measurements",
            pa.struct(
                [
                    pa.field("volume_count", pa.float64()),
                    pa.field("flow_vph", pa.float64()),
                    pa.field("speed_kph", pa.float64()),
                    pa.field("occupancy_percent", pa.float64()),
                    pa.field("sample_count", pa.int64()),
                ]
            ),
        ),
        pa.field(
            "quality",
            pa.struct([pa.field("quality_flags", pa.list_(pa.string()))]),
        ),
    ]
)

DATE_LEVEL_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("graph_version", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("local_date", pa.date32(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("interval_seconds", pa.int16(), nullable=False),
        pa.field("volume_count", pa.float64(), nullable=False),
        pa.field("flow_vph", pa.float64(), nullable=False),
        pa.field("speed_kph", pa.float64()),
        pa.field("occupancy_percent", pa.float64()),
        pa.field("reference_speed_kph", pa.float64(), nullable=False),
        pa.field("slowdown", pa.float64()),
        pa.field("station_count", pa.int32(), nullable=False),
        pa.field("detector_count", pa.int32(), nullable=False),
        pa.field("sample_count", pa.int64(), nullable=False),
        pa.field("source_station_ids_json", pa.string(), nullable=False),
        pa.field("quality_flags_json", pa.string(), nullable=False),
        pa.field("evidence_level", pa.string(), nullable=False),
        pa.field("calibration_status", pa.string(), nullable=False),
    ]
)

PROFILE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("graph_version", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("interval_seconds", pa.int16(), nullable=False),
        pa.field("source_window_start", pa.date32(), nullable=False),
        pa.field("source_window_end", pa.date32(), nullable=False),
        pa.field("sample_days", pa.int32(), nullable=False),
        pa.field("volume_sample_days", pa.int32(), nullable=False),
        pa.field("speed_sample_days", pa.int32(), nullable=False),
        pa.field("occupancy_sample_days", pa.int32(), nullable=False),
        pa.field("slowdown_sample_days", pa.int32(), nullable=False),
        *[
            pa.field(name, pa.float64())
            for name in (
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
        ],
        pa.field("source_station_ids_json", pa.string(), nullable=False),
        pa.field("quality_flags_json", pa.string(), nullable=False),
        pa.field("profile_status", pa.string(), nullable=False),
        pa.field("evidence_level", pa.string(), nullable=False),
        pa.field("calibration_status", pa.string(), nullable=False),
    ]
)


class HistoricalCalibrationCompileError(RuntimeError):
    """The frozen corpus cannot produce a reproducible Phase 1.3 artifact."""


@dataclass(slots=True)
class _StationAccumulator:
    volume_count: float = 0.0
    flow_vph: float = 0.0
    weighted_speed_sum: float = 0.0
    weighted_speed_volume: float = 0.0
    speeds: list[float] = field(default_factory=list)
    occupancy_sum: float = 0.0
    occupancy_count: int = 0
    sample_count: int = 0
    detector_ids: set[str] = field(default_factory=set)
    quality_flags: set[str] = field(default_factory=set)

    def add(
        self,
        *,
        detector_id: str,
        volume_count: float,
        flow_vph: float,
        speed_kph: float | None,
        occupancy_percent: float | None,
        sample_count: int,
        quality_flags: Iterable[str],
    ) -> None:
        self.volume_count += volume_count
        self.flow_vph += flow_vph
        self.sample_count += sample_count
        self.detector_ids.add(detector_id)
        self.quality_flags.update(quality_flags)
        if speed_kph is not None:
            self.speeds.append(speed_kph)
            if volume_count > 0:
                self.weighted_speed_sum += speed_kph * volume_count
                self.weighted_speed_volume += volume_count
        if occupancy_percent is not None:
            self.occupancy_sum += occupancy_percent
            self.occupancy_count += 1

    @property
    def speed_kph(self) -> float | None:
        if self.weighted_speed_volume > 0:
            return self.weighted_speed_sum / self.weighted_speed_volume
        return statistics.median(self.speeds) if self.speeds else None

    @property
    def occupancy_percent(self) -> float | None:
        return (
            self.occupancy_sum / self.occupancy_count if self.occupancy_count else None
        )


def compile_historical_calibration_v2(
    *,
    corpus_root: Path,
    campaign_manifest_path: Path,
    graph_edges_path: Path,
    graph_version: str,
    output_directory: Path,
    edge_partition_count: int = 128,
    json_block_size: int = 4 * 1024 * 1024,
    require_complete_calendar: bool = True,
    log: Log = print,
) -> HistoricalCalibrationCompilerManifestV1:
    """Compile exact-date edges and weekday distributions with bounded memory."""
    if edge_partition_count < 1:
        raise ValueError("edge_partition_count must be positive")
    if json_block_size < 64 * 1024:
        raise ValueError("json_block_size must be at least 64 KiB")
    corpus_root = corpus_root.resolve()
    output_directory = output_directory.resolve()
    if output_directory.exists():
        raise HistoricalCalibrationCompileError(
            "Output directory already exists; production artifacts are immutable"
        )
    corpus_path = corpus_root / "corpus-manifest.json"
    corpus = CalibrationObservationCorpusV2.model_validate_json(
        corpus_path.read_bytes()
    )
    _validate_source_corpus(corpus)
    campaign_bytes = campaign_manifest_path.read_bytes()
    campaign_sha256 = _sha256_bytes(campaign_bytes)
    campaign = json.loads(campaign_bytes)
    expected_dataset_version = f"campaign-manifest-sha256:{campaign_sha256}"
    if corpus.source_dataset.dataset_version != expected_dataset_version:
        raise HistoricalCalibrationCompileError(
            "Campaign manifest does not match the frozen source corpus"
        )
    chunks = campaign.get("chunks")
    if campaign.get("schema_version") != 2 or not isinstance(chunks, dict):
        raise HistoricalCalibrationCompileError("Expected campaign manifest schema 2")
    edge_speeds = _load_reference_speeds(graph_edges_path)
    graph_edges_sha256 = _sha256_file(graph_edges_path)
    grouped_shards, source_window_start, source_window_end = _group_shards(
        corpus_root, corpus, chunks
    )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    work = staging / ".aggregate-work"
    work.mkdir()
    date_path = staging / DATE_LEVEL_FILENAME
    profile_path = staging / PROFILE_FILENAME
    date_writer = parquet.ParquetWriter(
        date_path, DATE_LEVEL_SCHEMA, compression="zstd", use_dictionary=True
    )
    partition_writers: dict[int, parquet.ParquetWriter] = {}
    partition_paths: dict[int, Path] = {}
    source_observation_count = 0
    mapped_observation_count = 0
    unmapped_observation_count = 0
    date_row_count = 0
    weekday_counts: dict[str, int] = defaultdict(int)
    bucket_values: set[int] = set()
    try:
        for group_number, (week_start, shards) in enumerate(
            sorted(grouped_shards.items()), start=1
        ):
            station_groups: dict[tuple[Any, ...], _StationAccumulator] = {}
            for shard_path, shard in sorted(shards, key=lambda item: item[1].shard_id):
                stream_path = shard_path.parent / shard.observation_stream.relative_path
                for batch in _observation_batches(stream_path, json_block_size):
                    batch_source, batch_mapped, batch_unmapped = _accumulate_batch(
                        batch,
                        station_groups,
                        graph_version=graph_version,
                    )
                    source_observation_count += batch_source
                    mapped_observation_count += batch_mapped
                    unmapped_observation_count += batch_unmapped
            date_rows = _collapse_week(
                station_groups,
                edge_speeds=edge_speeds,
                graph_version=graph_version,
                source_corpus_digest=corpus.corpus_digest,
            )
            if date_rows:
                table = pa.Table.from_pylist(date_rows, schema=DATE_LEVEL_SCHEMA)
                date_writer.write_table(table)
                date_row_count += len(date_rows)
                for partition_id, rows in _partition_rows(
                    date_rows, edge_partition_count
                ).items():
                    partition_table = pa.Table.from_pylist(
                        rows, schema=DATE_LEVEL_SCHEMA
                    )
                    writer = partition_writers.get(partition_id)
                    if writer is None:
                        partition_path = work / f"edge-part-{partition_id:04d}.parquet"
                        writer = parquet.ParquetWriter(
                            partition_path,
                            DATE_LEVEL_SCHEMA,
                            compression="zstd",
                            use_dictionary=True,
                        )
                        partition_writers[partition_id] = writer
                        partition_paths[partition_id] = partition_path
                    writer.write_table(partition_table)
                for row in date_rows:
                    weekday_counts[str(row["weekday"])] += 1
                    bucket_values.add(int(row["bucket_start_minute"]))
            log(
                f"DATE LEVEL weeks={group_number:,}/{len(grouped_shards):,} "
                f"week={week_start} source={source_observation_count:,} "
                f"mapped={mapped_observation_count:,} rows={date_row_count:,}"
            )
        if source_observation_count != corpus.observation_count:
            raise HistoricalCalibrationCompileError(
                "Compiler source accounting does not equal the frozen corpus count"
            )
        if require_complete_calendar:
            if set(weekday_counts) != set(WEEKDAYS):
                raise HistoricalCalibrationCompileError(
                    "Production compilation did not represent all five weekdays"
                )
            if bucket_values != set(range(0, 1440, 15)):
                raise HistoricalCalibrationCompileError(
                    "Production compilation did not represent all 96 buckets"
                )
        date_writer.close()
        for writer in partition_writers.values():
            writer.close()

        profile_writer = parquet.ParquetWriter(
            profile_path, PROFILE_SCHEMA, compression="zstd", use_dictionary=True
        )
        profile_row_count = 0
        try:
            for partition_number, partition_path in sorted(partition_paths.items()):
                frame = parquet.read_table(partition_path).to_pandas()
                profile_rows = _aggregate_partition(
                    frame,
                    graph_version=graph_version,
                    source_corpus_digest=corpus.corpus_digest,
                )
                if profile_rows:
                    profile_writer.write_table(
                        pa.Table.from_pylist(profile_rows, schema=PROFILE_SCHEMA)
                    )
                    profile_row_count += len(profile_rows)
                log(
                    f"WEEKDAY PROFILES partitions={partition_number + 1:,}/"
                    f"{edge_partition_count:,} rows={profile_row_count:,}"
                )
        finally:
            profile_writer.close()
        shutil.rmtree(work)

        date_identity = _table_identity(date_path, DATE_LEVEL_FILENAME)
        profile_identity = _table_identity(profile_path, PROFILE_FILENAME)
        manifest_payload: dict[str, Any] = {
            "schema_version": HISTORICAL_CALIBRATION_COMPILER_SCHEMA_VERSION,
            "artifact_type": "commute_help_historical_calibration_compilation",
            "artifact_id": _artifact_id(corpus.corpus_digest, graph_version),
            "artifact_status": "historical_input",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC),
            "compiler": {
                "name": COMPILER_NAME,
                "code_version": COMPILER_CODE_VERSION,
                "model_version": None,
            },
            "source_corpus_digest": corpus.corpus_digest,
            "source_integrity_digest": corpus.integrity.index_digest,
            "source_campaign_manifest_sha256": campaign_sha256,
            "graph_version": graph_version,
            "graph_edges_sha256": graph_edges_sha256,
            "source_window_start": source_window_start,
            "source_window_end": source_window_end,
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "weekdays": WEEKDAYS,
            "possible_buckets_per_day": 96,
            "complete_calendar_required": require_complete_calendar,
            "edge_partition_count": edge_partition_count,
            "json_block_size_bytes": json_block_size,
            "aggregation_policy": AGGREGATION_POLICY.model_dump(mode="json"),
            "source_observation_count": source_observation_count,
            "mapped_observation_count": mapped_observation_count,
            "unmapped_observation_count": unmapped_observation_count,
            "date_level": date_identity,
            "weekday_profiles": profile_identity,
        }
        manifest_payload["content_digest"] = _content_digest(manifest_payload)
        manifest = HistoricalCalibrationCompilerManifestV1.model_validate(
            manifest_payload
        )
        report = _validation_report(
            manifest,
            weekday_counts=dict(sorted(weekday_counts.items())),
            bucket_values=sorted(bucket_values),
        )
        (staging / REPORT_JSON_FILENAME).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / REPORT_MARKDOWN_FILENAME).write_text(
            _validation_report_markdown(report), encoding="utf-8"
        )
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        os.replace(staging, output_directory)
        return manifest
    except BaseException:
        with suppress(OSError, pa.ArrowException):
            date_writer.close()
        for writer in partition_writers.values():
            with suppress(OSError, pa.ArrowException):
                writer.close()
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_source_corpus(corpus: CalibrationObservationCorpusV2) -> None:
    if corpus.materialization_state != "complete_unvalidated":
        raise HistoricalCalibrationCompileError(
            "Phase 1.3 requires a complete_unvalidated frozen corpus"
        )
    if corpus.calibration_status != "not_calibrated" or corpus.integrity is None:
        raise HistoricalCalibrationCompileError(
            "Source corpus evidence state is invalid"
        )
    if corpus.integrity.global_conflict_identity_count:
        raise HistoricalCalibrationCompileError(
            "Source corpus has unresolved conflicts"
        )
    if corpus.integrity.indexed_shard_count != len(corpus.shards):
        raise HistoricalCalibrationCompileError("Source corpus integrity is incomplete")


def _group_shards(
    corpus_root: Path,
    corpus: CalibrationObservationCorpusV2,
    chunks: dict[str, Any],
) -> tuple[dict[date, list[tuple[Path, CalibrationObservationShardV2]]], date, date]:
    groups: dict[date, list[tuple[Path, CalibrationObservationShardV2]]] = defaultdict(
        list
    )
    starts: list[date] = []
    ends: list[date] = []
    for entry in corpus.shards:
        manifest_path = corpus_root / entry.relative_path
        shard = CalibrationObservationShardV2.model_validate_json(
            manifest_path.read_bytes()
        )
        chunk = chunks.get(shard.source_partition_id)
        if not isinstance(chunk, dict):
            raise HistoricalCalibrationCompileError(
                f"Missing campaign chunk for {shard.source_partition_id}"
            )
        start = date.fromisoformat(str(chunk["start_date"]))
        end = date.fromisoformat(str(chunk["end_date"]))
        if (
            shard.shard_id != entry.shard_id
            or shard.content_digest != entry.content_digest
            or shard.observation_stream.canonical_sha256
            != entry.observation_stream_canonical_sha256
            or shard.observation_stream.physical_sha256
            != entry.observation_stream_physical_sha256
            or shard.audit.accepted_count != entry.accepted_count
        ):
            raise HistoricalCalibrationCompileError(
                f"Corpus/shard manifest mismatch for {entry.shard_id}"
            )
        groups[start].append((manifest_path, shard))
        starts.append(start)
        ends.append(end)
    if not groups:
        raise HistoricalCalibrationCompileError("Source corpus contains no shards")
    return groups, min(starts), max(ends)


def _load_reference_speeds(path: Path) -> dict[str, float]:
    table = parquet.read_table(path, columns=["edge_id", "maxspeed_kph"])
    frame = table.to_pandas()
    if frame["edge_id"].duplicated().any():
        raise HistoricalCalibrationCompileError("Graph edge IDs must be unique")
    speeds: dict[str, float] = {}
    for edge_id, speed in zip(frame["edge_id"], frame["maxspeed_kph"], strict=True):
        value = float(speed)
        if math.isfinite(value) and value > 0:
            speeds[str(edge_id)] = value
    if not speeds:
        raise HistoricalCalibrationCompileError("Graph has no positive edge speeds")
    return speeds


def _observation_batches(path: Path, block_size: int) -> Iterable[pa.RecordBatch]:
    reader = pajson.open_json(
        path,
        read_options=pajson.ReadOptions(use_threads=True, block_size=block_size),
        parse_options=pajson.ParseOptions(
            explicit_schema=OBSERVATION_JSON_SCHEMA,
            unexpected_field_behavior="ignore",
        ),
    )
    yield from reader


def _accumulate_batch(
    batch: pa.RecordBatch,
    station_groups: dict[tuple[Any, ...], _StationAccumulator],
    *,
    graph_version: str,
) -> tuple[int, int, int]:
    detector = batch.column("detector")
    calendar = batch.column("calendar")
    association = batch.column("road_association")
    measurements = batch.column("measurements")
    quality = batch.column("quality")
    station_ids = detector.field("station_id").to_pylist()
    detector_ids = detector.field("detector_id").to_pylist()
    exact_dates = calendar.field("exact_date").to_pylist()
    weekdays = calendar.field("weekday").to_pylist()
    starts = calendar.field("interval_start").to_pylist()
    associated_graphs = association.field("graph_version").to_pylist()
    edge_ids = association.field("app_edge_id").to_pylist()
    directions = batch.column("direction").to_pylist()
    volume_counts = measurements.field("volume_count").to_pylist()
    flows = measurements.field("flow_vph").to_pylist()
    speeds = measurements.field("speed_kph").to_pylist()
    occupancies = measurements.field("occupancy_percent").to_pylist()
    sample_counts = measurements.field("sample_count").to_pylist()
    flags = quality.field("quality_flags").to_pylist()
    mapped = 0
    for values in zip(
        station_ids,
        detector_ids,
        exact_dates,
        weekdays,
        starts,
        associated_graphs,
        edge_ids,
        directions,
        volume_counts,
        flows,
        speeds,
        occupancies,
        sample_counts,
        flags,
        strict=True,
    ):
        (
            station_id,
            detector_id,
            exact_date,
            weekday,
            interval_start,
            associated_graph,
            edge_id,
            direction,
            volume_count,
            flow_vph,
            speed_kph,
            occupancy_percent,
            sample_count,
            quality_flags,
        ) = values
        if edge_id is None:
            continue
        mapped += 1
        if associated_graph != graph_version:
            raise HistoricalCalibrationCompileError(
                f"Observation graph version {associated_graph!r} does not match {graph_version!r}"
            )
        observed_date = date.fromisoformat(str(exact_date))
        expected_weekday = (
            WEEKDAYS[observed_date.weekday()] if observed_date.weekday() < 5 else None
        )
        if weekday != expected_weekday:
            raise HistoricalCalibrationCompileError(
                f"Invalid or weekend calendar identity: {exact_date} / {weekday}"
            )
        minute = int(str(interval_start)[11:13]) * 60 + int(str(interval_start)[14:16])
        if minute % 15:
            raise HistoricalCalibrationCompileError(
                "Observation is not on a 15-minute bucket"
            )
        if volume_count is None or flow_vph is None:
            raise HistoricalCalibrationCompileError(
                "Mapped observation is missing volume/flow"
            )
        key = (
            observed_date,
            str(weekday),
            minute,
            str(edge_id),
            str(direction),
            str(station_id),
        )
        station_groups.setdefault(key, _StationAccumulator()).add(
            detector_id=str(detector_id),
            volume_count=float(volume_count),
            flow_vph=float(flow_vph),
            speed_kph=float(speed_kph) if speed_kph is not None else None,
            occupancy_percent=(
                float(occupancy_percent) if occupancy_percent is not None else None
            ),
            sample_count=int(sample_count),
            quality_flags=quality_flags or (),
        )
    return batch.num_rows, mapped, batch.num_rows - mapped


def _collapse_week(
    station_groups: dict[tuple[Any, ...], _StationAccumulator],
    *,
    edge_speeds: dict[str, float],
    graph_version: str,
    source_corpus_digest: str,
) -> list[dict[str, Any]]:
    edge_groups: dict[tuple[Any, ...], list[tuple[str, _StationAccumulator]]] = (
        defaultdict(list)
    )
    edge_directions: dict[tuple[Any, ...], str] = {}
    for key, accumulator in station_groups.items():
        observed_date, weekday, minute, edge_id, direction, station_id = key
        direction_key = (observed_date, weekday, minute, edge_id)
        previous_direction = edge_directions.setdefault(direction_key, direction)
        if previous_direction != direction:
            raise HistoricalCalibrationCompileError(
                f"Directed app edge {edge_id!r} has conflicting directions"
            )
        edge_groups[(observed_date, weekday, minute, edge_id, direction)].append(
            (station_id, accumulator)
        )
    rows: list[dict[str, Any]] = []
    for key, stations in sorted(edge_groups.items()):
        observed_date, weekday, minute, edge_id, direction = key
        reference_speed = edge_speeds.get(edge_id)
        if reference_speed is None:
            raise HistoricalCalibrationCompileError(
                f"Mapped app edge {edge_id!r} is absent from the graph edge artifact"
            )
        station_ids = sorted(station_id for station_id, _ in stations)
        volume_count = statistics.median(
            accumulator.volume_count for _, accumulator in stations
        )
        flow_vph = statistics.median(
            accumulator.flow_vph for _, accumulator in stations
        )
        speed_values = [
            value
            for _, accumulator in stations
            if (value := accumulator.speed_kph) is not None
        ]
        occupancy_values = [
            value
            for _, accumulator in stations
            if (value := accumulator.occupancy_percent) is not None
        ]
        speed = statistics.median(speed_values) if speed_values else None
        occupancy = statistics.median(occupancy_values) if occupancy_values else None
        flags = set().union(*(accumulator.quality_flags for _, accumulator in stations))
        if len(stations) > 1:
            flags.add("multiple_stations_median_not_flow_sum")
        if speed is None:
            flags.add("speed_missing_no_imputation")
        elif speed == 0:
            flags.add("zero_speed_slowdown_unavailable")
        if occupancy is None:
            flags.add("occupancy_missing_no_imputation")
        elif occupancy > 100:
            flags.add("occupancy_above_100_unresolved")
        slowdown = reference_speed / speed if speed is not None and speed > 0 else None
        rows.append(
            {
                "schema_version": 1,
                "record_id": _record_id(
                    "date",
                    source_corpus_digest,
                    graph_version,
                    edge_id,
                    observed_date.isoformat(),
                    str(minute),
                ),
                "app_edge_id": edge_id,
                "graph_version": graph_version,
                "direction": direction,
                "local_date": observed_date,
                "weekday": weekday,
                "bucket_start_minute": minute,
                "interval_seconds": 900,
                "volume_count": float(volume_count),
                "flow_vph": float(flow_vph),
                "speed_kph": float(speed) if speed is not None else None,
                "occupancy_percent": (
                    float(occupancy) if occupancy is not None else None
                ),
                "reference_speed_kph": reference_speed,
                "slowdown": float(slowdown) if slowdown is not None else None,
                "station_count": len(stations),
                "detector_count": sum(
                    len(accumulator.detector_ids) for _, accumulator in stations
                ),
                "sample_count": sum(
                    accumulator.sample_count for _, accumulator in stations
                ),
                "source_station_ids_json": json.dumps(
                    station_ids, separators=(",", ":")
                ),
                "quality_flags_json": json.dumps(sorted(flags), separators=(",", ":")),
                "evidence_level": "historical_observation_input_aggregated",
                "calibration_status": "not_calibrated",
            }
        )
    return rows


def _partition_rows(
    rows: list[dict[str, Any]], partition_count: int
) -> dict[int, list[dict[str, Any]]]:
    partitions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        digest = hashlib.sha256(str(row["app_edge_id"]).encode("utf-8")).digest()
        partition_id = int.from_bytes(digest[:8], "big") % partition_count
        partitions[partition_id].append(row)
    return partitions


def _aggregate_partition(
    frame: pd.DataFrame,
    *,
    graph_version: str,
    source_corpus_digest: str,
) -> list[dict[str, Any]]:
    date_identity = [
        "app_edge_id",
        "local_date",
        "weekday",
        "bucket_start_minute",
    ]
    if frame.duplicated(date_identity).any():
        raise HistoricalCalibrationCompileError(
            "Date-level partition contains duplicate edge/date/bucket identities"
        )
    frame = frame.sort_values(
        ["app_edge_id", "weekday", "bucket_start_minute", "local_date"],
        kind="mergesort",
    )
    rows: list[dict[str, Any]] = []
    grouped = frame.groupby(
        ["app_edge_id", "direction", "weekday", "bucket_start_minute"],
        sort=True,
        observed=True,
    )
    for (edge_id, direction, weekday, minute), values in grouped:
        station_ids = sorted(
            {
                station_id
                for encoded in values["source_station_ids_json"]
                for station_id in json.loads(encoded)
            }
        )
        flags = {
            "candidate_unvalidated",
            "exact_date_equal_weight",
            "no_outlier_threshold",
        }
        volume = _finite_series(values["volume_count"])
        flow = _finite_series(values["flow_vph"])
        speed = _finite_series(values["speed_kph"])
        occupancy = _finite_series(values["occupancy_percent"])
        slowdown = _finite_series(values["slowdown"])
        if len(speed) < len(values):
            flags.add("missing_speed_days_retained")
        if len(occupancy) < len(values):
            flags.add("missing_occupancy_days_retained")
        if len(slowdown) < len(values):
            flags.add("missing_slowdown_days_retained")
        rows.append(
            {
                "schema_version": 1,
                "record_id": _record_id(
                    "profile",
                    source_corpus_digest,
                    graph_version,
                    str(edge_id),
                    str(weekday),
                    str(int(minute)),
                ),
                "app_edge_id": str(edge_id),
                "graph_version": graph_version,
                "direction": str(direction),
                "weekday": str(weekday),
                "bucket_start_minute": int(minute),
                "interval_seconds": 900,
                "source_window_start": values["local_date"].min(),
                "source_window_end": values["local_date"].max(),
                "sample_days": len(values),
                "volume_sample_days": len(volume),
                "speed_sample_days": len(speed),
                "occupancy_sample_days": len(occupancy),
                "slowdown_sample_days": len(slowdown),
                **_metric_fields("volume_count", volume, (0.50, 0.85, 0.90, 0.95)),
                **_metric_fields("flow_vph", flow, (0.50, 0.85, 0.90, 0.95)),
                **_metric_fields("speed_kph", speed, (0.10, 0.50, 0.85, 0.90, 0.95)),
                **_metric_fields("occupancy_percent", occupancy, (0.50, 0.85, 0.95)),
                **_metric_fields(
                    "slowdown", slowdown, (0.50, 0.85, 0.90, 0.95), include_mean=False
                ),
                "source_station_ids_json": json.dumps(
                    station_ids, separators=(",", ":")
                ),
                "quality_flags_json": json.dumps(sorted(flags), separators=(",", ":")),
                "profile_status": "candidate",
                "evidence_level": "derived_historical_profile_input",
                "calibration_status": "not_calibrated",
            }
        )
    return rows


def _finite_series(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric[numeric.map(math.isfinite)]


def _metric_fields(
    prefix: str,
    values: pd.Series,
    quantiles: tuple[float, ...],
    *,
    include_mean: bool = True,
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    if include_mean:
        result[f"{prefix}_mean"] = float(values.mean()) if len(values) else None
    for quantile in quantiles:
        name = f"{prefix}_p{round(quantile * 100)}"
        result[name] = float(values.quantile(quantile)) if len(values) else None
    return result


def _table_identity(path: Path, relative_path: str) -> dict[str, Any]:
    metadata = parquet.read_metadata(path)
    return {
        "relative_path": relative_path,
        "schema_version": 1,
        "sha256": _sha256_file(path),
        "byte_count": path.stat().st_size,
        "row_count": metadata.num_rows,
    }


def _validation_report(
    manifest: HistoricalCalibrationCompilerManifestV1,
    *,
    weekday_counts: dict[str, int],
    bucket_values: list[int],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "compiler": manifest.compiler.model_dump(mode="json"),
        "artifact_content_digest": manifest.content_digest,
        "source_corpus_digest": manifest.source_corpus_digest,
        "source_integrity_digest": manifest.source_integrity_digest,
        "source_window": {
            "start": manifest.source_window_start.isoformat(),
            "end": manifest.source_window_end.isoformat(),
        },
        "weekdays_represented": sorted(
            weekday_counts, key=lambda value: WEEKDAY_INDEX[value]
        ),
        "date_level_rows_by_weekday": weekday_counts,
        "bucket_count": len(bucket_values),
        "bucket_start_minutes": bucket_values,
        "complete_calendar_required": manifest.complete_calendar_required,
        "edge_partition_count": manifest.edge_partition_count,
        "json_block_size_bytes": manifest.json_block_size_bytes,
        "source_observation_count": manifest.source_observation_count,
        "mapped_observation_count": manifest.mapped_observation_count,
        "unmapped_observation_count": manifest.unmapped_observation_count,
        "date_level": manifest.date_level.model_dump(mode="json"),
        "weekday_profiles": manifest.weekday_profiles.model_dump(mode="json"),
        "aggregation_policy": manifest.aggregation_policy.model_dump(mode="json"),
        "calibration_status": "not_calibrated",
        "limitations": [
            "Only accepted app-edge associations contribute edge artifacts.",
            "No sample-day promotion threshold or outlier filter is applied.",
            "Speed zero is preserved but cannot produce a slowdown ratio.",
            "Occupancy above 100 is preserved and flagged rather than repaired.",
            "Candidate profiles are historical input, not historically calibrated traffic.",
        ],
    }


def _validation_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Historical calibration-v2 validation report",
        "",
        f"- Compiler: `{report['compiler']['name']} / {report['compiler']['code_version']}`",
        f"- Source corpus: `{report['source_corpus_digest']}`",
        f"- Integrity: `{report['source_integrity_digest']}`",
        f"- Source observations: **{report['source_observation_count']:,}**",
        f"- Mapped observations: **{report['mapped_observation_count']:,}**",
        f"- Date-level rows: **{report['date_level']['row_count']:,}**",
        f"- Weekday profile rows: **{report['weekday_profiles']['row_count']:,}**",
        f"- Weekdays: **{', '.join(report['weekdays_represented'])}**",
        f"- Observed 15-minute buckets: **{report['bucket_count']} / 96**",
        "- Calibration status: **not_calibrated**",
        "",
        "## Limitations",
        "",
        *[f"- {item}" for item in report["limitations"]],
        "",
    ]
    return "\n".join(lines)


def _artifact_id(corpus_digest: str, graph_version: str) -> str:
    digest = hashlib.sha256(
        f"{COMPILER_CODE_VERSION}|{corpus_digest}|{graph_version}".encode()
    ).hexdigest()
    return f"portal.historical-calibration.{digest[:40]}"


def _record_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join((kind, *parts)).encode("utf-8")).hexdigest()
    return f"portal.{kind}.{digest[:48]}"


def _content_digest(payload: dict[str, Any]) -> str:
    reproducible = dict(payload)
    reproducible.pop("generated_at", None)
    reproducible.pop("content_digest", None)
    return _sha256_bytes(canonical_json(reproducible).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
