"""Versioned 24/7 traffic schedule loading and smooth local-time interpolation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")
INTERPOLATED_COLUMNS = (
    "volume_mean",
    "volume_median",
    "volume_std",
    "volume_p10",
    "volume_p50",
    "volume_p85",
    "volume_p90",
    "volume_p95",
    "speed_mean",
    "speed_median",
    "speed_std",
    "speed_p10",
    "speed_p50",
    "speed_p85",
    "speed_p90",
    "speed_p95",
    "occupancy_mean",
    "occupancy_median",
)


class TrafficScheduleError(RuntimeError):
    pass


def day_type_for(value: datetime) -> str:
    weekday = value.weekday()
    if weekday <= 3:
        return "mon_thu"
    if weekday == 4:
        return "friday"
    if weekday == 5:
        return "saturday"
    return "sunday"


class TrafficScheduleService:
    def __init__(self, schedule_path: Path, manifest_path: Path) -> None:
        self.schedule_path = schedule_path
        self.manifest_path = manifest_path
        self.manifest: dict[str, Any] | None = None
        self.schedule: pd.DataFrame | None = None

    def load(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if _sha256(self.schedule_path) != manifest["artifact"]["sha256"]:
            raise TrafficScheduleError("Traffic schedule checksum does not match its manifest")
        schedule = pd.read_parquet(self.schedule_path)
        if set(schedule["profile_version"].unique()) != {manifest["schedule_version"]}:
            raise TrafficScheduleError("Schedule rows and manifest versions differ")
        self.manifest = manifest
        self.schedule = schedule.set_index(
            ["day_type", "bucket_start_minute", "detector_id", "app_edge_id"]
        ).sort_index()

    def state_at(
        self, value: datetime, app_edge_ids: set[str] | None = None
    ) -> dict[str, Any]:
        if self.schedule is None or self.manifest is None:
            raise TrafficScheduleError("Traffic schedule service has not been loaded")
        if value.tzinfo is None:
            raise TrafficScheduleError("Schedule queries require a timezone-aware datetime")
        local = value.astimezone(LOCAL_TIMEZONE)
        lower_minute = (local.hour * 60 + local.minute) // 15 * 15
        lower_time = local.replace(
            hour=lower_minute // 60,
            minute=lower_minute % 60,
            second=0,
            microsecond=0,
        )
        upper_time = lower_time + timedelta(minutes=15)
        fraction = (local - lower_time).total_seconds() / 900.0
        lower = self._bucket(day_type_for(lower_time), lower_minute, app_edge_ids)
        upper = self._bucket(
            day_type_for(upper_time),
            upper_time.hour * 60 + upper_time.minute,
            app_edge_ids,
        )
        joined = lower.merge(
            upper,
            on=["detector_id", "app_edge_id"],
            suffixes=("_lower", "_upper"),
            how="inner",
            validate="one_to_one",
        )
        output = joined[["detector_id", "app_edge_id"]].copy()
        for column in INTERPOLATED_COLUMNS:
            output[column] = (
                joined[f"{column}_lower"]
                + (joined[f"{column}_upper"] - joined[f"{column}_lower"]) * fraction
            )
        output["volume_mean"] = output["volume_mean"].clip(lower=0)
        output["speed_mean"] = output["speed_mean"].clip(lower=0.1, upper=160)
        output["evidence_level"] = [
            _combined_evidence(lower_level, upper_level)
            for lower_level, upper_level in zip(
                joined["evidence_level_lower"], joined["evidence_level_upper"], strict=True
            )
        ]
        output["quality_flags"] = [
            sorted(set(_flags(a)) | set(_flags(b)))
            for a, b in zip(
                joined["quality_flags_lower"], joined["quality_flags_upper"], strict=True
            )
        ]
        return {
            "schedule_version": self.manifest["schedule_version"],
            "requested_at": local.isoformat(),
            "lower_bucket": {
                "day_type": day_type_for(lower_time),
                "minute": lower_minute,
            },
            "upper_bucket": {
                "day_type": day_type_for(upper_time),
                "minute": upper_time.hour * 60 + upper_time.minute,
            },
            "interpolation_fraction": fraction,
            "rows": output.to_dict(orient="records"),
        }

    def _bucket(
        self, day_type: str, minute: int, app_edge_ids: set[str] | None
    ) -> pd.DataFrame:
        assert self.schedule is not None
        try:
            frame = self.schedule.xs((day_type, minute), level=(0, 1)).reset_index()
        except KeyError as error:
            raise TrafficScheduleError(
                f"Schedule has no {day_type} bucket at minute {minute}"
            ) from error
        if app_edge_ids is not None:
            frame = frame[frame["app_edge_id"].isin(app_edge_ids)]
        return frame


def _combined_evidence(first: str, second: str) -> str:
    order = {
        "observed_historical_input": 0,
        "modeled_gap_fill": 1,
        "modeled_unobserved": 2,
    }
    return max((first, second), key=lambda item: order.get(item, 99))


def _flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [value]
    return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
