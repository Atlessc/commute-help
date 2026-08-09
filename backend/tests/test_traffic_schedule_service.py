"""Focused interpolation and evidence tests for the 24/7 schedule."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.app.services.traffic_schedule_service import (
    INTERPOLATED_COLUMNS,
    TrafficScheduleError,
    TrafficScheduleService,
)


def _service(tmp_path) -> TrafficScheduleService:
    rows = []
    day_offsets = {"mon_thu": 0.0, "friday": 100.0, "saturday": 200.0, "sunday": 300.0}
    for day_type, offset in day_offsets.items():
        for minute in range(0, 1440, 15):
            row = {
                "profile_version": "fixture-v1",
                "day_type": day_type,
                "bucket_start_minute": minute,
                "detector_id": "station-1",
                "app_edge_id": "edge-1",
                "evidence_level": (
                    "modeled_unobserved"
                    if day_type in {"saturday", "sunday"}
                    else "observed_historical_input"
                ),
                "quality_flags": '["fixture"]',
            }
            row.update({column: offset + minute for column in INTERPOLATED_COLUMNS})
            rows.append(row)
    schedule_path = tmp_path / "schedule.parquet"
    pd.DataFrame(rows).to_parquet(schedule_path, index=False)
    digest = hashlib.sha256(schedule_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "schedule-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schedule_version": "fixture-v1",
                "artifact": {"sha256": digest},
            }
        ),
        encoding="utf-8",
    )
    service = TrafficScheduleService(schedule_path, manifest_path)
    service.load()
    return service


def test_interpolates_between_quarter_hour_buckets(tmp_path) -> None:
    service = _service(tmp_path)
    state = service.state_at(
        datetime(2026, 8, 3, 7, 22, tzinfo=ZoneInfo("America/Los_Angeles"))
    )
    assert state["lower_bucket"] == {"day_type": "mon_thu", "minute": 435}
    assert state["upper_bucket"] == {"day_type": "mon_thu", "minute": 450}
    assert state["interpolation_fraction"] == pytest.approx(7 / 15)
    assert state["rows"][0]["volume_mean"] == pytest.approx(442.0)
    assert state["rows"][0]["evidence_level"] == "observed_historical_input"


def test_midnight_interpolation_crosses_day_type_and_reports_fallback(tmp_path) -> None:
    service = _service(tmp_path)
    state = service.state_at(
        datetime(2026, 8, 7, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles"))
    )
    assert state["lower_bucket"] == {"day_type": "friday", "minute": 1425}
    assert state["upper_bucket"] == {"day_type": "saturday", "minute": 0}
    assert state["rows"][0]["evidence_level"] == "modeled_unobserved"


def test_rejects_naive_time(tmp_path) -> None:
    service = _service(tmp_path)
    with pytest.raises(TrafficScheduleError, match="timezone-aware"):
        service.state_at(datetime(2026, 8, 3, 7, 22))
