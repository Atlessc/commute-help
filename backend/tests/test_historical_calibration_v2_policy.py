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
from backend.app.schemas.historical_calibration_v2_policy import (
    HistoricalQualityPolicyManifestV1,
)
from backend.app.schemas.historical_calibration_v2_quality import (
    QualityCharacterizationManifestV1,
)
from backend.app.services.historical_calibration_v2_compiler import AGGREGATION_POLICY
from backend.app.services.historical_calibration_v2_policy_service import (
    MANIFEST_OUTPUT,
    PROFILE_STATUS_OUTPUT,
    REPORT_JSON_OUTPUT,
    apply_historical_calibration_v2_quality_policy,
    classify_date_support,
)
from backend.app.services.historical_calibration_v2_quality_service import (
    CHARACTERIZATION_SCHEMA,
    COUNT_FIELDS,
    FRACTION_FIELDS,
    PROFILE_DISTRIBUTION_COLUMNS,
    SUPPORT_STAT_FIELDS,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _characterization_row(
    *,
    weekday: str,
    possible: int,
    observed: int,
    speed_present: int | None = None,
    zero_speed: int = 0,
    occupancy_above_100: int = 0,
    zero_flow: int = 0,
) -> dict[str, object]:
    speed_present = observed if speed_present is None else speed_present
    row: dict[str, object] = {field.name: None for field in CHARACTERIZATION_SCHEMA}
    for column in COUNT_FIELDS:
        row[column] = 0
    for column in FRACTION_FIELDS:
        row[column] = None
    for column in SUPPORT_STAT_FIELDS:
        row[column] = None
    for column in PROFILE_DISTRIBUTION_COLUMNS:
        row[column] = None
    row.update(
        {
            "schema_version": 1,
            "record_id": f"characterization-{weekday}",
            "app_edge_id": "app-edge-a",
            "direction": "north",
            "weekday": weekday,
            "bucket_start_minute": 480,
            "possible_date_count": possible,
            "observed_date_count": observed,
            "missing_date_count": possible - observed,
            "coverage_fraction": observed / possible,
            "present_flow_date_count": observed,
            "missing_flow_date_count": possible - observed,
            "zero_flow_date_count": zero_flow,
            "zero_flow_fraction_present": zero_flow / observed,
            "present_speed_date_count": speed_present,
            "missing_speed_date_count": possible - speed_present,
            "zero_speed_date_count": zero_speed,
            "zero_speed_fraction_present": zero_speed / speed_present,
            "present_occupancy_date_count": observed,
            "missing_occupancy_date_count": possible - observed,
            "occupancy_above_100_date_count": occupancy_above_100,
            "occupancy_above_100_fraction_present": occupancy_above_100 / observed,
            "sample_count_present_date_count": observed,
            "sample_count_missing_date_count": possible - observed,
            "sample_count_zero_date_count": 0,
            "zero_speed_positive_flow_date_count": zero_speed,
            "zero_speed_zero_flow_date_count": 0,
            "zero_speed_occupancy_present_date_count": zero_speed,
            "source_station_ids_json": '["station-a"]',
            "source_profile_quality_flags_json": "[]",
            "source_profile_status": "candidate_unvalidated",
            "source_evidence_level": "historical_input",
            "calibration_status": "not_calibrated",
            "characterization_status": "descriptive_only",
            "occupancy_percent_p95": 150.0 if occupancy_above_100 else 50.0,
        }
    )
    for prefix in ("sample_count", "detector_support_count", "station_support_count"):
        for suffix in (
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
        ):
            row[f"{prefix}_{suffix}"] = {
                "sample_count": 45.0,
                "detector_support_count": 2.0,
                "station_support_count": 1.0,
            }[prefix]
    return row


def _policy_inputs(root: Path) -> tuple[Path, Path]:
    candidate = root / "candidate"
    characterization = root / "characterization"
    candidate.mkdir()
    characterization.mkdir()

    source_manifest = HistoricalCalibrationCompilerManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_compilation",
            "artifact_id": "synthetic.phase-1.3.v1",
            "artifact_status": "historical_input",
            "calibration_status": "not_calibrated",
            "generated_at": datetime(2026, 2, 1, tzinfo=UTC),
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
            "source_window_start": date(2026, 1, 1),
            "source_window_end": date(2026, 1, 31),
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "weekdays": ("monday", "tuesday", "wednesday", "thursday", "friday"),
            "possible_buckets_per_day": 96,
            "complete_calendar_required": False,
            "edge_partition_count": 1,
            "json_block_size_bytes": 65_536,
            "aggregation_policy": AGGREGATION_POLICY.model_dump(mode="json"),
            "source_observation_count": 5,
            "mapped_observation_count": 5,
            "unmapped_observation_count": 0,
            "date_level": {
                "relative_path": "edge-day-15m.parquet",
                "schema_version": 1,
                "sha256": "6" * 64,
                "byte_count": 0,
                "row_count": 5,
            },
            "weekday_profiles": {
                "relative_path": "edge-time-distributions.parquet",
                "schema_version": 1,
                "sha256": "7" * 64,
                "byte_count": 0,
                "row_count": 5,
            },
            "content_digest": "8" * 64,
        }
    )
    source_manifest_path = candidate / "historical-calibration-manifest.json"
    source_manifest_path.write_text(
        canonical_json(source_manifest.model_dump(mode="json")), encoding="utf-8"
    )

    rows = [
        _characterization_row(
            weekday="monday",
            possible=10,
            observed=9,
            speed_present=8,
            zero_speed=1,
            occupancy_above_100=1,
            zero_flow=1,
        ),
        _characterization_row(weekday="tuesday", possible=4, observed=3),
        _characterization_row(weekday="wednesday", possible=4, observed=2),
        _characterization_row(weekday="thursday", possible=3, observed=3, zero_speed=3),
        _characterization_row(
            weekday="friday", possible=20, observed=18, occupancy_above_100=18
        ),
    ]
    profile_path = characterization / "profile-quality-characterization.parquet"
    parquet.write_table(
        pa.Table.from_pylist(rows, schema=CHARACTERIZATION_SCHEMA), profile_path
    )
    report_path = characterization / "quality-characterization.json"
    report_path.write_text(
        json.dumps(
            {
                "sample_count": {
                    "distribution": {
                        "count": 35,
                        "min": 1,
                        "p50": 45,
                        "p95": 90,
                        "max": 180,
                    },
                    "bands": [{"band": "1-9", "date_row_count": 1}],
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    markdown_path = characterization / "QUALITY_CHARACTERIZATION.md"
    markdown_path.write_text("synthetic\n", encoding="utf-8")
    source_bytes = source_manifest_path.read_bytes()
    quality_manifest = QualityCharacterizationManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_quality_characterization",
            "evidence_status": "descriptive_characterization_only",
            "calibration_status": "not_calibrated",
            "source_profile_status": "candidate_unvalidated",
            "generated_at": datetime(2026, 2, 2, tzinfo=UTC),
            "analyzer": {
                "name": "historical_calibration_v2_quality_characterizer",
                "code_version": "phase-1.3-quality-v1",
                "model_version": None,
            },
            "methodology_version": "phase-1.3-quality-v1",
            "methodology": {
                "grain": "app_edge_id_weekday_15_minute_bucket",
                "possible_dates": "inclusive_source_window_calendar",
                "measurement_missingness": "possible_dates_minus_present_measurement_dates",
                "quantiles": "exact_linear_within_profile",
                "aggregation": "descriptive_only_no_eligibility_policy",
                "edge_partitioning": "contiguous_sorted_edge_ranges",
            },
            "source_phase_1_3_manifest_sha256": hashlib.sha256(
                source_bytes
            ).hexdigest(),
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_corpus_digest": source_manifest.source_corpus_digest,
            "source_integrity_digest": source_manifest.source_integrity_digest,
            "graph_version": source_manifest.graph_version,
            "input_date_level": {
                "relative_path": "edge-day-15m.parquet",
                "sha256": "9" * 64,
                "byte_count": 0,
                "row_count": 5,
            },
            "input_weekday_profiles": {
                "relative_path": "edge-time-distributions.parquet",
                "sha256": "a" * 64,
                "byte_count": 0,
                "row_count": 5,
            },
            "output_profile_characterization": {
                "relative_path": profile_path.name,
                "sha256": _sha(profile_path),
                "byte_count": profile_path.stat().st_size,
                "row_count": 5,
            },
            "output_json": {
                "relative_path": report_path.name,
                "sha256": _sha(report_path),
                "byte_count": report_path.stat().st_size,
                "row_count": None,
            },
            "output_markdown": {
                "relative_path": markdown_path.name,
                "sha256": _sha(markdown_path),
                "byte_count": markdown_path.stat().st_size,
                "row_count": None,
            },
            "content_digest": "b" * 64,
        }
    )
    (characterization / "quality-characterization-manifest.json").write_text(
        canonical_json(quality_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return candidate, characterization


def test_date_support_policy_uses_exact_possible_date_ratio() -> None:
    assert classify_date_support(122, 135) == "direct_calibration_evidence"
    assert classify_date_support(121, 135) == "supplemental_evidence"
    assert classify_date_support(9, 10) == "direct_calibration_evidence"
    assert classify_date_support(3, 4) == "supplemental_evidence"
    assert classify_date_support(2, 4) == "insufficient_direct_evidence"
    assert classify_date_support(3, 3) == "direct_calibration_evidence"


def test_policy_is_measurement_specific_retains_sparse_profiles_and_is_deterministic(
    tmp_path: Path,
) -> None:
    candidate, characterization = _policy_inputs(tmp_path)
    source_hash_before = _sha(
        characterization / "profile-quality-characterization.parquet"
    )
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = apply_historical_calibration_v2_quality_policy(
        candidate_directory=candidate,
        characterization_directory=characterization,
        output_directory=first_dir,
    )
    second = apply_historical_calibration_v2_quality_policy(
        candidate_directory=candidate,
        characterization_directory=characterization,
        output_directory=second_dir,
    )

    frame = pd.read_parquet(first_dir / PROFILE_STATUS_OUTPUT)
    assert len(frame) == 5
    assert set(frame["weekday"]) == {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    }
    assert not {"saturday", "sunday"} & set(frame["weekday"])
    assert not frame.duplicated(["app_edge_id", "weekday", "bucket_start_minute"]).any()
    monday = frame[frame["weekday"] == "monday"].iloc[0]
    tuesday = frame[frame["weekday"] == "tuesday"].iloc[0]
    wednesday = frame[frame["weekday"] == "wednesday"].iloc[0]
    thursday = frame[frame["weekday"] == "thursday"].iloc[0]
    friday = frame[frame["weekday"] == "friday"].iloc[0]
    assert monday["date_support_class"] == "direct_calibration_evidence"
    assert tuesday["date_support_class"] == "supplemental_evidence"
    assert wednesday["date_support_class"] == "insufficient_direct_evidence"
    assert not wednesday["flow_direct_calibration_eligible"]
    assert monday["flow_direct_calibration_eligible"]
    assert monday["speed_direct_calibration_eligible"]
    assert monday["occupancy_direct_calibration_eligible"]
    assert monday["occupancy_above_100_excluded_date_count"] == 1
    assert thursday["flow_direct_calibration_eligible"]
    assert not thursday["speed_direct_calibration_eligible"]
    assert thursday["speed_zero_diagnostic_date_count"] == 3
    assert friday["flow_direct_calibration_eligible"]
    assert friday["speed_direct_calibration_eligible"]
    assert not friday["occupancy_direct_calibration_eligible"]
    assert friday["occupancy_eligible_date_count"] == 0
    assert friday["occupancy_above_100_excluded_date_count"] == 18
    assert set(frame["sample_count_policy"]) == {"diagnostic_only_no_global_threshold"}
    assert set(frame["calibration_status"]) == {"not_calibrated"}
    assert "historically_calibrated" not in set(frame["calibration_status"])
    assert source_hash_before == _sha(
        characterization / "profile-quality-characterization.parquet"
    )
    source_frame = pd.read_parquet(
        characterization / "profile-quality-characterization.parquet"
    )
    assert source_frame["occupancy_percent_p95"].max() == 150

    report = json.loads((first_dir / REPORT_JSON_OUTPUT).read_text())
    classes = {
        row["status"]: row["profile_count"]
        for row in report["date_support"]["class_counts"]
    }
    assert classes == {
        "direct_calibration_evidence": 3,
        "insufficient_direct_evidence": 1,
        "supplemental_evidence": 1,
    }
    assert report["flow"]["direct_calibration_profile_count"] == 3
    assert report["speed"]["direct_calibration_profile_count"] == 2
    assert report["occupancy"]["direct_calibration_profile_count"] == 2
    assert report["occupancy"]["above_100_excluded_sample_count"] == 19
    assert report["source_support"]["global_minimum_sample_count"] is None
    assert first.content_digest == second.content_digest
    assert first.output_profile_status.sha256 == second.output_profile_status.sha256
    assert first.output_report_json.sha256 == second.output_report_json.sha256
    assert first.output_report_markdown.sha256 == second.output_report_markdown.sha256
    HistoricalQualityPolicyManifestV1.model_validate_json(
        (first_dir / MANIFEST_OUTPUT).read_bytes()
    )


def test_policy_interruption_cannot_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, characterization = _policy_inputs(tmp_path)
    output = tmp_path / "policy"

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("synthetic policy interruption")

    monkeypatch.setattr(
        "backend.app.services.historical_calibration_v2_policy_service._write_report_files",
        interrupt,
    )
    with pytest.raises(RuntimeError, match="synthetic policy interruption"):
        apply_historical_calibration_v2_quality_policy(
            candidate_directory=candidate,
            characterization_directory=characterization,
            output_directory=output,
        )
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*"))
