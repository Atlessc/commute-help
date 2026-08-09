"""Bounded-memory compiler for the general 15-minute PORTAL schedule."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import pyarrow.dataset as pads


Log = Callable[[str], None]
BUCKETS = np.arange(0, 1440, 15, dtype=np.int16)
DAY_TYPES = ("mon_thu", "friday")
QUANTILES = (0.10, 0.50, 0.85, 0.90, 0.95)


@dataclass(frozen=True)
class HistogramSpec:
    width: float
    bins: int
    maximum: float


VOLUME_HISTOGRAM = HistogramSpec(width=25.0, bins=241, maximum=6000.0)
SPEED_HISTOGRAM = HistogramSpec(width=1.0, bins=161, maximum=160.0)
OCCUPANCY_HISTOGRAM = HistogramSpec(width=0.5, bins=201, maximum=100.0)


def compile_traffic_schedule(
    *,
    observations_directory: Path,
    station_matches_path: Path,
    schedule_version: str,
    log: Log = print,
    batch_size: int = 262_144,
) -> tuple[pd.DataFrame, dict]:
    matches = pd.read_parquet(station_matches_path)
    matches = matches[matches["status"] == "accepted"][["station_id", "edge_id"]]
    matches = matches.drop_duplicates(subset=["station_id"], keep=False).sort_values(
        "station_id"
    )
    if matches.empty:
        raise ValueError("No uniquely accepted PORTAL station matches are available")
    station_ids = matches["station_id"].astype(str).tolist()
    edge_ids = matches["edge_id"].astype(str).tolist()
    station_index = {station_id: index for index, station_id in enumerate(station_ids)}
    station_count = len(station_ids)
    group_count = station_count * len(DAY_TYPES) * len(BUCKETS)
    count = np.zeros(group_count, dtype=np.int64)
    volume_sum = np.zeros(group_count, dtype=np.float64)
    volume_sumsq = np.zeros(group_count, dtype=np.float64)
    speed_sum = np.zeros(group_count, dtype=np.float64)
    speed_sumsq = np.zeros(group_count, dtype=np.float64)
    occupancy_sum = np.zeros(group_count, dtype=np.float64)
    occupancy_count = np.zeros(group_count, dtype=np.int64)
    volume_hist = np.zeros((group_count, VOLUME_HISTOGRAM.bins), dtype=np.uint32)
    speed_hist = np.zeros((group_count, SPEED_HISTOGRAM.bins), dtype=np.uint32)
    occupancy_hist = np.zeros(
        (group_count, OCCUPANCY_HISTOGRAM.bins), dtype=np.uint32
    )
    station_dates: set[int] = set()
    scanned_rows = 0
    accepted_rows = 0

    dataset = pads.dataset(observations_directory, format="parquet", partitioning="hive")
    scanner = dataset.scanner(
        columns=[
            "station_or_segment_id",
            "local_date",
            "iso_weekday",
            "minute_of_day",
            "volume",
            "speed_kph",
            "occupancy",
            "volume_valid",
            "speed_valid",
            "occupancy_valid",
            "profile_eligible",
        ],
        batch_size=batch_size,
        use_threads=True,
    )
    for batch_number, batch in enumerate(scanner.to_batches(), start=1):
        frame = batch.to_pandas()
        scanned_rows += len(frame)
        frame["station_index"] = frame["station_or_segment_id"].map(station_index)
        frame = frame[
            frame["station_index"].notna()
            & frame["profile_eligible"]
            & frame["volume_valid"]
            & frame["speed_valid"]
            & frame["iso_weekday"].between(1, 5)
        ]
        if frame.empty:
            continue
        accepted_rows += len(frame)
        stations = frame["station_index"].to_numpy(dtype=np.int64)
        day_indices = (frame["iso_weekday"].to_numpy(dtype=np.int16) == 5).astype(
            np.int64
        )
        bucket_indices = frame["minute_of_day"].to_numpy(dtype=np.int16) // 15
        groups = (stations * 2 + day_indices) * 96 + bucket_indices
        volumes = frame["volume"].to_numpy(dtype=np.float64)
        speeds = frame["speed_kph"].to_numpy(dtype=np.float64)
        occupancies = frame["occupancy"].to_numpy(dtype=np.float64)
        occupancy_valid = frame["occupancy_valid"].to_numpy(dtype=bool) & np.isfinite(
            occupancies
        )
        np.add.at(count, groups, 1)
        np.add.at(volume_sum, groups, volumes)
        np.add.at(volume_sumsq, groups, volumes * volumes)
        np.add.at(speed_sum, groups, speeds)
        np.add.at(speed_sumsq, groups, speeds * speeds)
        np.add.at(occupancy_sum, groups[occupancy_valid], occupancies[occupancy_valid])
        np.add.at(occupancy_count, groups[occupancy_valid], 1)
        _add_histogram(volume_hist, groups, volumes, VOLUME_HISTOGRAM)
        _add_histogram(speed_hist, groups, speeds, SPEED_HISTOGRAM)
        _add_histogram(
            occupancy_hist,
            groups[occupancy_valid],
            occupancies[occupancy_valid],
            OCCUPANCY_HISTOGRAM,
        )
        dates = pd.to_datetime(frame["local_date"]).to_numpy(dtype="datetime64[D]").astype(
            np.int64
        )
        encoded_dates = (stations * 2 + day_indices) * 50_000 + dates
        station_dates.update(np.unique(encoded_dates).tolist())
        if batch_number % 200 == 0:
            log(
                f"STREAM batches={batch_number:,} scanned={scanned_rows:,} "
                f"accepted={accepted_rows:,}"
            )

    log("CALCULATE bounded histogram quantiles")
    volume_quantiles = _histogram_quantiles(volume_hist, count, VOLUME_HISTOGRAM)
    speed_quantiles = _histogram_quantiles(speed_hist, count, SPEED_HISTOGRAM)
    occupancy_quantiles = _histogram_quantiles(
        occupancy_hist, occupancy_count, OCCUPANCY_HISTOGRAM, quantiles=(0.5,)
    )
    expected_days: Counter[int] = Counter(value // 50_000 for value in station_dates)
    rows: list[dict] = []
    unusable_station_ids: list[str] = []
    for station_position, (station_id, edge_id) in enumerate(
        zip(station_ids, edge_ids, strict=True)
    ):
        day_payloads: list[dict] = []
        for day_index, day_type in enumerate(DAY_TYPES):
            start = (station_position * 2 + day_index) * 96
            stop = start + 96
            group_slice = slice(start, stop)
            expected = expected_days[station_position * 2 + day_index]
            metrics = {
                "volume_mean": _mean(volume_sum[group_slice], count[group_slice]),
                "volume_median": volume_quantiles[1][group_slice],
                "volume_std": _std(
                    volume_sum[group_slice], volume_sumsq[group_slice], count[group_slice]
                ),
                "volume_p10": volume_quantiles[0][group_slice],
                "volume_p50": volume_quantiles[1][group_slice],
                "volume_p85": volume_quantiles[2][group_slice],
                "volume_p90": volume_quantiles[3][group_slice],
                "volume_p95": volume_quantiles[4][group_slice],
                "speed_mean": _mean(speed_sum[group_slice], count[group_slice]),
                "speed_median": speed_quantiles[1][group_slice],
                "speed_std": _std(
                    speed_sum[group_slice], speed_sumsq[group_slice], count[group_slice]
                ),
                "speed_p10": speed_quantiles[0][group_slice],
                "speed_p50": speed_quantiles[1][group_slice],
                "speed_p85": speed_quantiles[2][group_slice],
                "speed_p90": speed_quantiles[3][group_slice],
                "speed_p95": speed_quantiles[4][group_slice],
                "occupancy_mean": _mean(
                    occupancy_sum[group_slice], occupancy_count[group_slice]
                ),
                "occupancy_median": occupancy_quantiles[0][group_slice],
            }
            observed = count[group_slice] > 0
            day_payloads.append(
                {
                    "day_index": day_index,
                    "day_type": day_type,
                    "start": start,
                    "expected": expected,
                    "metrics": metrics,
                    "observed": observed,
                }
            )
        if not any(payload["observed"].any() for payload in day_payloads):
            unusable_station_ids.append(station_id)
            continue
        for payload_index, payload in enumerate(day_payloads):
            whole_day_fallback = not payload["observed"].any()
            source_day_type: str | None = None
            if whole_day_fallback:
                source = day_payloads[1 - payload_index]
                if not source["observed"].any():
                    raise ValueError(f"Station {station_id} has no usable weekday observations")
                payload["metrics"] = {
                    name: values.copy() for name, values in source["metrics"].items()
                }
                source_day_type = source["day_type"]
            metric_defaulted = False
            for name, values in payload["metrics"].items():
                finite = np.isfinite(values)
                if finite.any():
                    payload["metrics"][name] = _cyclic_fill(values, finite)
                else:
                    payload["metrics"][name] = np.zeros(len(BUCKETS), dtype=np.float64)
                    metric_defaulted = True
            start = payload["start"]
            expected = payload["expected"]
            metrics = payload["metrics"]
            for bucket_index, minute in enumerate(BUCKETS):
                samples = int(count[start + bucket_index])
                flags = ["bounded_histogram_quantiles"]
                evidence = "observed_historical_input"
                fallback_source = None
                if whole_day_fallback:
                    flags.append("whole_day_type_cross_weekday_fallback")
                    evidence = "modeled_gap_fill"
                    fallback_source = f"{source_day_type}_historical_shape_unscaled"
                elif samples == 0:
                    flags.append("missing_bucket_cyclic_interpolation")
                    evidence = "modeled_gap_fill"
                if metric_defaulted:
                    flags.append("metric_unavailable_default_zero")
                rows.append(
                    {
                        "profile_version": schedule_version,
                        "day_type": payload["day_type"],
                        "bucket_start_minute": int(minute),
                        "detector_id": station_id,
                        "app_edge_id": edge_id,
                        **{name: float(values[bucket_index]) for name, values in metrics.items()},
                        "sample_count": samples,
                        "valid_day_count": samples,
                        "missing_percent": (
                            round(100 * max(0.0, 1 - samples / expected), 3)
                            if expected
                            else 100.0
                        ),
                        "quality_flags": json.dumps(flags, separators=(",", ":")),
                        "evidence_level": evidence,
                        "fallback_source": fallback_source,
                    }
                )
    observed_schedule = pd.DataFrame.from_records(rows)
    weekend = observed_schedule[observed_schedule["day_type"] == "mon_thu"].copy()
    weekend["sample_count"] = 0
    weekend["valid_day_count"] = 0
    weekend["missing_percent"] = 100.0
    weekend["evidence_level"] = "modeled_unobserved"
    weekend["fallback_source"] = "mon_thu_historical_shape_unscaled"
    weekend["quality_flags"] = weekend["quality_flags"].map(
        lambda value: json.dumps(
            sorted(set(json.loads(value)) | {"weekend_without_observations"}),
            separators=(",", ":"),
        )
    )
    saturday = weekend.copy()
    saturday["day_type"] = "saturday"
    sunday = weekend.copy()
    sunday["day_type"] = "sunday"
    schedule = pd.concat([observed_schedule, saturday, sunday], ignore_index=True)
    schedule = schedule.sort_values(
        ["day_type", "bucket_start_minute", "detector_id", "app_edge_id"]
    ).reset_index(drop=True)
    report = {
        "schedule_version": schedule_version,
        "scanned_rows": scanned_rows,
        "accepted_observation_rows": accepted_rows,
        "matched_station_count": station_count,
        "accepted_station_count": int(schedule["detector_id"].nunique()),
        "unusable_matched_station_count": len(unusable_station_ids),
        "unique_app_edge_count": int(schedule["app_edge_id"].nunique()),
        "schedule_rows": len(schedule),
        "buckets_per_day_type": int(schedule["bucket_start_minute"].nunique()),
        "day_types": sorted(schedule["day_type"].unique().tolist()),
        "evidence_counts": schedule["evidence_level"].value_counts().to_dict(),
        "quantile_method": "bounded_fixed_histogram",
        "weekend_policy": "modeled_unobserved_mon_thu_shape_unscaled",
    }
    return schedule, report


def _add_histogram(
    histogram: np.ndarray,
    groups: np.ndarray,
    values: np.ndarray,
    spec: HistogramSpec,
) -> None:
    bins = np.clip(np.floor(np.clip(values, 0, spec.maximum) / spec.width), 0, spec.bins - 1)
    flat = groups * spec.bins + bins.astype(np.int64)
    np.add.at(histogram.reshape(-1), flat, 1)


def _histogram_quantiles(
    histogram: np.ndarray,
    counts: np.ndarray,
    spec: HistogramSpec,
    quantiles: tuple[float, ...] = QUANTILES,
) -> list[np.ndarray]:
    outputs = [np.full(len(counts), np.nan, dtype=np.float64) for _ in quantiles]
    for start in range(0, len(counts), 4096):
        stop = min(start + 4096, len(counts))
        cumulative = np.cumsum(histogram[start:stop], axis=1, dtype=np.uint32)
        local_counts = counts[start:stop]
        for output, quantile in zip(outputs, quantiles, strict=True):
            targets = np.maximum(1, np.ceil(local_counts * quantile)).astype(np.uint32)
            indices = (cumulative >= targets[:, None]).argmax(axis=1)
            values = (indices.astype(np.float64) + 0.5) * spec.width
            values[local_counts == 0] = np.nan
            output[start:stop] = values
    return outputs


def _mean(sums: np.ndarray, counts: np.ndarray) -> np.ndarray:
    return np.divide(sums, counts, out=np.full_like(sums, np.nan), where=counts > 0)


def _std(sums: np.ndarray, sumsq: np.ndarray, counts: np.ndarray) -> np.ndarray:
    means = _mean(sums, counts)
    variance = np.divide(sumsq, counts, out=np.zeros_like(sums), where=counts > 0) - means**2
    return np.sqrt(np.maximum(variance, 0))


def _cyclic_fill(values: np.ndarray, observed: np.ndarray) -> np.ndarray:
    if observed.all():
        return values
    positions = np.arange(len(values), dtype=np.float64)
    known_indices = np.flatnonzero(observed & np.isfinite(values))
    if not len(known_indices):
        raise ValueError("A station/day type has no usable observations")
    result = values.copy()
    missing = ~np.isfinite(result)
    result[missing] = np.interp(
        positions[missing],
        known_indices.astype(np.float64),
        values[known_indices],
        period=len(values),
    )
    return result
