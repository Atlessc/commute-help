"""Bounded compilation and local registration of finalized PORTAL traffic profiles."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pyarrow_dataset
import pyarrow.parquet as parquet

from backend.app.db.database import DatabaseManager
from backend.app.services.portal_station_matcher import MATCHER_VERSION


LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")
COMPILER_VERSION = "portal-background-profile-compiler-v1"
COMPILED_SCHEMA_VERSION = 1
PROFILE_PERIODS = {
    "weekday_morning": (5 * 60, 10 * 60),
    "weekday_afternoon": (14 * 60, 19 * 60),
}
REQUIRED_OBSERVATION_COLUMNS = {
    "station_or_segment_id",
    "timestamp_local",
    "local_date",
    "local_month",
    "iso_weekday",
    "minute_of_day",
    "speed_kph",
    "volume",
    "profile_eligible",
}


class PortalProfileCompileError(RuntimeError):
    """The processed campaign cannot produce a reproducible background profile."""


def compile_profiles(
    *,
    observations_directory: Path,
    matches_path: Path,
    edges_path: Path,
    output_directory: Path,
    graph_version: str,
    source_campaign: str,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stream fall weekday observations into compact directed edge/time profiles."""

    emit = log or (lambda _message: None)
    matches = pd.read_parquet(matches_path)
    accepted = matches.loc[matches["status"] == "accepted"].copy()
    if accepted.empty:
        raise PortalProfileCompileError("No station matches passed the automatic gate.")
    station_to_edge = dict(zip(accepted["station_id"], accepted["edge_id"], strict=True))

    edges = pd.read_parquet(
        edges_path,
        columns=["edge_id", "maxspeed_kph", "estimated_capacity_vph"],
    )
    edges["edge_id"] = edges["edge_id"].astype(str)
    edge_speed = edges.set_index("edge_id")["maxspeed_kph"].to_dict()
    edge_capacity = edges.set_index("edge_id")["estimated_capacity_vph"].to_dict()
    missing_edge_ids = sorted(set(station_to_edge.values()) - set(edge_speed))
    if missing_edge_ids:
        raise PortalProfileCompileError(
            f"{len(missing_edge_ids)} accepted matches reference missing graph edges."
        )

    dataset = pyarrow_dataset.dataset(
        observations_directory,
        format="parquet",
        partitioning="hive",
    )
    missing_columns = sorted(REQUIRED_OBSERVATION_COLUMNS - set(dataset.schema.names))
    if missing_columns:
        raise PortalProfileCompileError(
            "Finalized observations are missing: " + ", ".join(missing_columns) + "."
        )

    selected_columns = sorted(REQUIRED_OBSERVATION_COLUMNS)
    filter_expression = pyarrow_dataset.field("local_month").isin([9, 10])
    bucket_writers: dict[tuple[str, int], parquet.ParquetWriter] = {}
    bucket_paths: dict[tuple[str, int], Path] = {}
    scanned_fall_rows = 0
    selected_period_rows = 0
    matched_rows = 0
    excluded_match_rows = 0
    date_minimum: dict[str, str | None] = defaultdict(lambda: None)
    date_maximum: dict[str, str | None] = defaultdict(lambda: None)

    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="commute-help-portal-profile-", dir=output_directory
    ) as temporary_name:
        temporary_directory = Path(temporary_name)
        emit("Streaming September/October weekday observations in bounded batches")
        try:
            for batch_number, batch in enumerate(
                dataset.to_batches(
                    columns=selected_columns,
                    filter=filter_expression,
                    batch_size=131_072,
                ),
                start=1,
            ):
                frame = batch.to_pandas()
                scanned_fall_rows += len(frame)
                frame = frame.loc[
                    frame["profile_eligible"].fillna(False).astype(bool)
                    & pd.to_numeric(frame["iso_weekday"], errors="coerce").between(1, 5)
                ].copy()
                frame["minute_of_day"] = pd.to_numeric(
                    frame["minute_of_day"], errors="coerce"
                )
                frame["period"] = frame["minute_of_day"].map(_period_for_minute)
                frame = frame.loc[frame["period"].notna()].copy()
                selected_period_rows += len(frame)
                frame["edge_id"] = frame["station_or_segment_id"].map(station_to_edge)
                excluded_match_rows += int(frame["edge_id"].isna().sum())
                frame = frame.loc[frame["edge_id"].notna()].copy()
                matched_rows += len(frame)
                if frame.empty:
                    continue
                frame["speed_kph"] = pd.to_numeric(frame["speed_kph"], errors="coerce")
                frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
                frame["edge_maxspeed_kph"] = frame["edge_id"].map(edge_speed)
                frame["multiplier"] = (
                    frame["edge_maxspeed_kph"] / frame["speed_kph"]
                ).clip(lower=1.0, upper=5.0)
                frame["local_date"] = frame["local_date"].astype(str).str.slice(0, 10)
                for period, period_frame in frame.groupby("period", sort=False):
                    dates = period_frame["local_date"]
                    minimum = str(dates.min())
                    maximum = str(dates.max())
                    date_minimum[str(period)] = _minimum_text(
                        date_minimum[str(period)], minimum
                    )
                    date_maximum[str(period)] = _maximum_text(
                        date_maximum[str(period)], maximum
                    )
                for (period, minute), bucket_frame in frame.groupby(
                    ["period", "minute_of_day"], sort=False
                ):
                    key = (str(period), int(minute))
                    compact = bucket_frame[
                        [
                            "station_or_segment_id",
                            "local_date",
                            "edge_id",
                            "speed_kph",
                            "volume",
                            "multiplier",
                        ]
                    ]
                    table = pa.Table.from_pandas(compact, preserve_index=False)
                    writer = bucket_writers.get(key)
                    if writer is None:
                        path = temporary_directory / f"{key[0]}-{key[1]:04d}.parquet"
                        writer = parquet.ParquetWriter(path, table.schema, compression="zstd")
                        bucket_writers[key] = writer
                        bucket_paths[key] = path
                    writer.write_table(table)
                if batch_number % 10 == 0:
                    emit(
                        f"Scanned {scanned_fall_rows:,} fall rows; "
                        f"retained {matched_rows:,} matched AM/PM rows"
                    )
        finally:
            for writer in bucket_writers.values():
                writer.close()

        emit("Collapsing each time bucket to typical directed edge conditions")
        aggregate_records: list[dict[str, Any]] = []
        multiplier_arrays: dict[str, list[np.ndarray]] = defaultdict(list)
        profile_counts: CounterLike = defaultdict(int)
        profile_volume_counts: CounterLike = defaultdict(int)
        profile_edges: dict[str, set[str]] = defaultdict(set)
        for (period, minute), path in sorted(bucket_paths.items()):
            frame = pd.read_parquet(path)
            frame = frame.sort_values(["edge_id", "local_date", "station_or_segment_id"])
            multiplier_values = pd.to_numeric(
                frame["multiplier"], errors="coerce"
            ).dropna().to_numpy(dtype=float)
            if len(multiplier_values):
                multiplier_arrays[period].append(multiplier_values)
            profile_counts[period] += int(frame["speed_kph"].gt(0).sum())
            profile_volume_counts[period] += int(frame["volume"].ge(0).sum())
            profile_edges[period].update(frame["edge_id"].astype(str).unique())
            for edge_id, values in frame.groupby("edge_id", sort=True):
                speeds = pd.to_numeric(values["speed_kph"], errors="coerce")
                volumes = pd.to_numeric(values["volume"], errors="coerce")
                valid_speeds = speeds[speeds.gt(0)]
                valid_volumes = volumes[volumes.ge(0)]
                aggregate_records.append(
                    {
                        "artifact_schema_version": COMPILED_SCHEMA_VERSION,
                        "period": period,
                        "bucket_minute": int(minute),
                        "edge_id": str(edge_id),
                        "speed_kph": _median_or_nan(valid_speeds),
                        "volume": _median_or_nan(valid_volumes),
                        "observation_count": int(len(values)),
                        "station_count": int(values["station_or_segment_id"].nunique()),
                        "edge_maxspeed_kph": float(edge_speed[str(edge_id)]),
                        "estimated_capacity_vph": float(edge_capacity[str(edge_id)]),
                    }
                )

    artifact = pd.DataFrame.from_records(aggregate_records).sort_values(
        ["period", "bucket_minute", "edge_id"], ignore_index=True
    )
    if artifact.empty:
        raise PortalProfileCompileError("No compact background rows were produced.")
    artifact_path = output_directory / "edge-bucket-profiles.parquet"
    _write_parquet_atomic(artifact_path, artifact)
    artifact_sha256 = _sha256_file(artifact_path)

    profiles: list[dict[str, Any]] = []
    for period in PROFILE_PERIODS:
        arrays = multiplier_arrays.get(period, [])
        if not arrays:
            continue
        multipliers = np.concatenate(arrays)
        multipliers = multipliers[np.isfinite(multipliers)]
        if not len(multipliers):
            continue
        version = hashlib.sha256(
            (
                f"{COMPILER_VERSION}|{MATCHER_VERSION}|{graph_version}|"
                f"{source_campaign}|{artifact_sha256}|{period}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        source_window = f"{date_minimum[period]} to {date_maximum[period]}"
        profiles.append(
            {
                "id": str(uuid5(NAMESPACE_URL, f"commute-help:{version}")),
                "name": (
                    "PORTAL Sep/Oct weekday morning"
                    if period == "weekday_morning"
                    else "PORTAL Sep/Oct weekday afternoon"
                ),
                "version": version,
                "source_name": "PSU PORTAL finalized local campaign",
                "source_window": source_window,
                "period": period,
                "graph_version": graph_version,
                "observation_count": int(profile_counts[period]),
                "stats": {
                    "compiler_version": COMPILER_VERSION,
                    "matcher_version": MATCHER_VERSION,
                    "artifact_schema_version": COMPILED_SCHEMA_VERSION,
                    "artifact_sha256": artifact_sha256,
                    "volume_observation_count": int(profile_volume_counts[period]),
                    "matched_edge_count": len(profile_edges[period]),
                    "median_multiplier": float(np.median(multipliers)),
                    "p85_multiplier": float(np.quantile(multipliers, 0.85)),
                    "p90_multiplier": float(np.quantile(multipliers, 0.90)),
                    "p95_multiplier": float(np.quantile(multipliers, 0.95)),
                    "multiplier_samples": _bounded_samples(multipliers),
                },
            }
        )

    report = {
        "schema_version": COMPILED_SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "matcher_version": MATCHER_VERSION,
        "graph_version": graph_version,
        "source_campaign": source_campaign,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "artifact_rows": len(artifact),
        "observed_station_count": len(matches),
        "accepted_station_count": len(accepted),
        "excluded_station_count": len(matches) - len(accepted),
        "scanned_fall_rows": scanned_fall_rows,
        "selected_period_rows": selected_period_rows,
        "matched_period_rows": matched_rows,
        "excluded_match_rows": excluded_match_rows,
        "profiles": profiles,
        "limitations": [
            "Only automatically accepted station matches contribute observations.",
            "PORTAL observes selected freeways and ramps; uncovered roads remain structurally modeled.",
            "Profiles describe typical September/October weekdays, not live traffic or incidents.",
            "Multiple stations mapped to one graph edge are combined by median rather than summed.",
        ],
    }
    _write_json_atomic(output_directory / "profile-build-report.json", report)
    _write_text_atomic(
        output_directory / "profile-build-report.md", _profile_report_markdown(report)
    )
    return report


def register_profiles(
    *,
    report: dict[str, Any],
    database_path: Path,
    campaign_manifest_path: Path,
    match_report_path: Path,
) -> None:
    """Idempotently register compiled artifacts so the existing selectors can list them."""

    database = DatabaseManager(database_path)
    database.initialize()
    artifact_path = Path(str(report["artifact_path"])).resolve()
    campaign_sha256 = _sha256_file(campaign_manifest_path)
    match_report = json.loads(match_report_path.read_text(encoding="utf-8"))
    import_version = hashlib.sha256(
        (
            f"{COMPILER_VERSION}|{campaign_sha256}|{report['artifact_sha256']}|"
            f"{report['graph_version']}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    import_id = str(uuid5(NAMESPACE_URL, f"commute-help:portal-import:{import_version}"))
    now = datetime.now(LOCAL_TIMEZONE).isoformat()
    quality = {
        "compiler_version": COMPILER_VERSION,
        "match_report": str(match_report_path.resolve()),
        "observed_station_count": report["observed_station_count"],
        "accepted_station_count": report["accepted_station_count"],
        "review_station_count": match_report["review_count"],
        "unmatched_station_count": match_report["unmatched_count"],
        "outside_graph_region_count": match_report.get("outside_graph_region_count", 0),
        "selected_period_rows": report["selected_period_rows"],
        "accepted_period_rows": report["matched_period_rows"],
        "excluded_period_rows": report["excluded_match_rows"],
        "limitations": report["limitations"],
    }
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO traffic_imports (
                id, source_name, original_filename, raw_path, raw_sha256,
                normalized_path, graph_version, imported_at, row_count,
                accepted_count, rejected_count, quality_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                normalized_path = excluded.normalized_path,
                imported_at = excluded.imported_at,
                row_count = excluded.row_count,
                accepted_count = excluded.accepted_count,
                rejected_count = excluded.rejected_count,
                quality_json = excluded.quality_json
            """,
            (
                import_id,
                "PSU PORTAL finalized local campaign",
                campaign_manifest_path.name,
                str(campaign_manifest_path.resolve()),
                campaign_sha256,
                str(artifact_path),
                report["graph_version"],
                now,
                report["selected_period_rows"],
                report["matched_period_rows"],
                report["excluded_match_rows"],
                json.dumps(quality, separators=(",", ":")),
            ),
        )
        for profile in report["profiles"]:
            connection.execute(
                """
                INSERT INTO traffic_profiles (
                    id, import_id, name, version, source_name, source_window,
                    period, graph_version, observation_count, stats_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET
                    name = excluded.name,
                    source_name = excluded.source_name,
                    source_window = excluded.source_window,
                    observation_count = excluded.observation_count,
                    stats_json = excluded.stats_json
                """,
                (
                    profile["id"],
                    import_id,
                    profile["name"],
                    profile["version"],
                    profile["source_name"],
                    profile["source_window"],
                    profile["period"],
                    profile["graph_version"],
                    profile["observation_count"],
                    json.dumps(profile["stats"], separators=(",", ":")),
                    now,
                ),
            )
        connection.commit()


CounterLike = dict[str, int]


def _period_for_minute(value: Any) -> str | None:
    try:
        minute = int(value)
    except (TypeError, ValueError):
        return None
    for period, (start, end) in PROFILE_PERIODS.items():
        if start <= minute < end:
            return period
    return None


def _median_or_nan(values: pd.Series) -> float:
    return float(values.median()) if len(values) else math.nan


def _bounded_samples(values: np.ndarray) -> list[float]:
    ordered = np.sort(values)
    if len(ordered) > 10_000:
        indices = np.linspace(0, len(ordered) - 1, num=10_000, dtype=int)
        ordered = ordered[indices]
    return [round(float(value), 6) for value in ordered]


def _minimum_text(current: str | None, value: str) -> str:
    return value if current is None or value < current else current


def _maximum_text(current: str | None, value: str) -> str:
    return value if current is None or value > current else current


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_parquet_atomic(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)


def _write_text_atomic(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True))


def _profile_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# PORTAL background profile build report",
        "",
        f"- Compiler: `{report['compiler_version']}`",
        f"- Matcher: `{report['matcher_version']}`",
        f"- Graph: `{report['graph_version']}`",
        f"- Accepted stations: **{report['accepted_station_count']:,} / {report['observed_station_count']:,}**",
        f"- Matched AM/PM observations: **{report['matched_period_rows']:,} / {report['selected_period_rows']:,}**",
        f"- Compact edge/bucket rows: **{report['artifact_rows']:,}**",
        "",
        "## Profiles",
        "",
        "| Profile | Source window | Observations | Volume rows | Matched edges | Median | P90 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for profile in report["profiles"]:
        stats = profile["stats"]
        lines.append(
            f"| {profile['name']} | {profile['source_window']} | {profile['observation_count']:,} | "
            f"{stats['volume_observation_count']:,} | {stats['matched_edge_count']:,} | "
            f"{stats['median_multiplier']:.3f}x | {stats['p90_multiplier']:.3f}x |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)
