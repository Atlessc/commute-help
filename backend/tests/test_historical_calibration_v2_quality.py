from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest
from pyarrow import parquet

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_quality import (
    QualityCharacterizationManifestV1,
)
from backend.app.services import historical_calibration_v2_quality_service as quality
from backend.app.services.historical_calibration_v2_compiler import (
    AGGREGATION_POLICY,
    DATE_LEVEL_SCHEMA,
    PROFILE_SCHEMA,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _date_row(
    local_date: date,
    weekday: str,
    *,
    flow: float,
    speed: float | None,
    occupancy: float | None,
    sample_count: int,
) -> dict[str, object]:
    slowdown = speed / 100 if speed is not None and speed > 0 else None
    return {
        "schema_version": 1,
        "record_id": f"date-{local_date.isoformat()}",
        "app_edge_id": "app-edge-a",
        "graph_version": "synthetic-graph-v1",
        "direction": "north",
        "local_date": local_date,
        "weekday": weekday,
        "bucket_start_minute": 480,
        "interval_seconds": 900,
        "volume_count": flow / 4,
        "flow_vph": flow,
        "speed_kph": speed,
        "occupancy_percent": occupancy,
        "reference_speed_kph": 100,
        "slowdown": slowdown,
        "station_count": 1,
        "detector_count": 2,
        "sample_count": sample_count,
        "source_station_ids_json": '["station-a"]',
        "quality_flags_json": "[]",
        "evidence_level": "historical_input",
        "calibration_status": "not_calibrated",
    }


def _source_artifact(root: Path) -> Path:
    root.mkdir()
    rows = [
        _date_row(
            date(2026, 1, 5),
            "monday",
            flow=0,
            speed=50,
            occupancy=10,
            sample_count=10,
        ),
        _date_row(
            date(2026, 1, 6),
            "tuesday",
            flow=40,
            speed=0,
            occupancy=101,
            sample_count=5,
        ),
        _date_row(
            date(2026, 1, 13),
            "tuesday",
            flow=44,
            speed=None,
            occupancy=20,
            sample_count=20,
        ),
        _date_row(
            date(2026, 1, 7),
            "wednesday",
            flow=20,
            speed=30,
            occupancy=None,
            sample_count=15,
        ),
        _date_row(
            date(2026, 1, 8),
            "thursday",
            flow=10,
            speed=50,
            occupancy=10,
            sample_count=8,
        ),
        _date_row(
            date(2026, 1, 15),
            "thursday",
            flow=20,
            speed=60,
            occupancy=110,
            sample_count=12,
        ),
        _date_row(
            date(2026, 1, 9),
            "friday",
            flow=30,
            speed=40,
            occupancy=30,
            sample_count=0,
        ),
    ]
    date_path = root / "edge-day-15m.parquet"
    parquet.write_table(pa.Table.from_pylist(rows, schema=DATE_LEVEL_SCHEMA), date_path)
    date_frame = pd.DataFrame(rows)
    profiles: list[dict[str, object]] = []
    distribution_columns = [
        field.name
        for field in PROFILE_SCHEMA
        if field.name.endswith(("_mean", "_p10", "_p50", "_p85", "_p90", "_p95"))
    ]
    for weekday, values in date_frame.groupby("weekday", sort=False):
        speed_days = int(values["speed_kph"].notna().sum())
        occupancy_days = int(values["occupancy_percent"].notna().sum())
        profile: dict[str, object] = {
            "schema_version": 1,
            "record_id": f"profile-{weekday}",
            "app_edge_id": "app-edge-a",
            "graph_version": "synthetic-graph-v1",
            "direction": "north",
            "weekday": weekday,
            "bucket_start_minute": 480,
            "interval_seconds": 900,
            "source_window_start": date(2026, 1, 5),
            "source_window_end": date(2026, 1, 16),
            "sample_days": len(values),
            "volume_sample_days": len(values),
            "speed_sample_days": speed_days,
            "occupancy_sample_days": occupancy_days,
            "slowdown_sample_days": int(values["slowdown"].notna().sum()),
            "source_station_ids_json": '["station-a"]',
            "quality_flags_json": "[]",
            "profile_status": "candidate",
            "evidence_level": "historical_input",
            "calibration_status": "not_calibrated",
        }
        profile.update(dict.fromkeys(distribution_columns, None))
        profile["flow_vph_mean"] = float(values["flow_vph"].mean())
        profile["speed_kph_mean"] = (
            float(values["speed_kph"].mean()) if speed_days else None
        )
        profile["occupancy_percent_mean"] = (
            float(values["occupancy_percent"].mean()) if occupancy_days else None
        )
        profiles.append(profile)
    profile_path = root / "edge-time-distributions.parquet"
    parquet.write_table(
        pa.Table.from_pylist(profiles, schema=PROFILE_SCHEMA), profile_path
    )
    manifest = HistoricalCalibrationCompilerManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_compilation",
            "artifact_id": "synthetic.phase-1.3.v1",
            "artifact_status": "historical_input",
            "calibration_status": "not_calibrated",
            "generated_at": datetime(2026, 1, 20, tzinfo=UTC),
            "compiler": {
                "name": "historical_calibration_v2_compiler",
                "code_version": "phase-1.3-v1",
                "model_version": None,
            },
            "source_corpus_digest": "1" * 64,
            "source_integrity_digest": "2" * 64,
            "source_campaign_manifest_sha256": "3" * 64,
            "graph_version": "synthetic-graph-v1",
            "graph_edges_sha256": "4" * 64,
            "source_window_start": date(2026, 1, 5),
            "source_window_end": date(2026, 1, 16),
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "weekdays": quality.WEEKDAYS,
            "possible_buckets_per_day": 96,
            "complete_calendar_required": False,
            "edge_partition_count": 1,
            "json_block_size_bytes": 65_536,
            "aggregation_policy": AGGREGATION_POLICY.model_dump(mode="json"),
            "source_observation_count": len(rows),
            "mapped_observation_count": len(rows),
            "unmapped_observation_count": 0,
            "date_level": {
                "relative_path": date_path.name,
                "schema_version": 1,
                "sha256": _sha(date_path),
                "byte_count": date_path.stat().st_size,
                "row_count": len(rows),
            },
            "weekday_profiles": {
                "relative_path": profile_path.name,
                "schema_version": 1,
                "sha256": _sha(profile_path),
                "byte_count": profile_path.stat().st_size,
                "row_count": len(profiles),
            },
            "content_digest": "5" * 64,
        }
    )
    (root / "historical-calibration-manifest.json").write_text(
        canonical_json(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return root


def test_quality_characterization_is_descriptive_complete_and_deterministic(
    tmp_path: Path,
) -> None:
    source = _source_artifact(tmp_path / "source")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = quality.characterize_historical_calibration_v2_quality(
        source_directory=source,
        output_directory=first_dir,
        edge_partition_count=2,
        batch_size=2,
        log=lambda _message: None,
    )
    second = quality.characterize_historical_calibration_v2_quality(
        source_directory=source,
        output_directory=second_dir,
        edge_partition_count=2,
        batch_size=3,
        log=lambda _message: None,
    )

    frame = pd.read_parquet(first_dir / quality.PROFILE_OUTPUT)
    assert len(frame) == 5
    assert not frame.duplicated(list(quality.PROFILE_KEY)).any()
    assert set(frame["weekday"]) == set(quality.WEEKDAYS)
    assert not {"saturday", "sunday"} & set(frame["weekday"])
    assert not {"eligible", "accepted", "trusted", "rejected"} & set(frame.columns)
    assert set(frame["characterization_status"]) == {"descriptive_only"}
    assert set(frame["calibration_status"]) == {"not_calibrated"}
    assert set(frame["source_profile_status"]) == {"candidate_unvalidated"}
    monday = frame[frame["weekday"] == "monday"].iloc[0]
    tuesday = frame[frame["weekday"] == "tuesday"].iloc[0]
    wednesday = frame[frame["weekday"] == "wednesday"].iloc[0]
    assert monday["possible_date_count"] == 2
    assert monday["observed_date_count"] == 1
    assert monday["coverage_fraction"] == pytest.approx(0.5)
    assert monday["zero_flow_date_count"] == 1
    assert tuesday["zero_speed_date_count"] == 1
    assert tuesday["missing_speed_date_count"] == 1
    assert tuesday["occupancy_above_100_date_count"] == 1
    assert wednesday["missing_occupancy_date_count"] == 2

    report = json.loads((first_dir / quality.JSON_OUTPUT).read_text())
    assert report["zero_speed"]["exact_date_zero_speed_rows"] == 1
    assert report["occupancy_above_100"]["exact_date_rows_above_100"] == 2
    assert report["sample_count"]["zero_date_rows"] == 1
    assert report["input"]["observed_exact_date_count"] == 7
    assert report["edge_support"]["represented_edge_count"] == 1
    edge = report["edge_support"]["all_edges"][0]
    assert edge["candidate_possible_date_rows"] == 10
    assert edge["theoretical_dense_date_rows"] == 960
    assert edge["within_candidate_date_coverage_fraction"] == pytest.approx(0.7)
    assert edge["calendar_coverage_fraction"] == pytest.approx(7 / 960)
    assert first.content_digest == second.content_digest
    assert (
        first.output_profile_characterization.sha256
        == second.output_profile_characterization.sha256
    )
    assert first.output_json.sha256 == second.output_json.sha256
    assert first.output_markdown.sha256 == second.output_markdown.sha256
    QualityCharacterizationManifestV1.model_validate_json(
        (first_dir / quality.MANIFEST_OUTPUT).read_bytes()
    )


def test_quality_characterization_interruption_cannot_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_artifact(tmp_path / "source")
    output = tmp_path / "quality"

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("synthetic characterization interruption")

    monkeypatch.setattr(quality, "_characterize_partition", interrupt)
    with pytest.raises(RuntimeError, match="synthetic characterization interruption"):
        quality.characterize_historical_calibration_v2_quality(
            source_directory=source,
            output_directory=output,
            edge_partition_count=2,
            batch_size=2,
            log=lambda _message: None,
        )
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_quality_manifest_requires_source_provenance() -> None:
    with pytest.raises(ValueError):
        QualityCharacterizationManifestV1.model_validate(
            {
                "schema_version": 1,
                "artifact_type": "commute_help_historical_calibration_quality_characterization",
            }
        )
