from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
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
from backend.app.services.historical_calibration_v2_p95_tail_service import (
    MANIFEST_OUTPUT,
    PM_END_MINUTE,
    PM_START_MINUTE,
    PROFILE_OUTPUT,
    P95TailAnalysisError,
    _analyze_modern,
    _build_modern_frame,
    analyze_historical_calibration_v2_p95_tail,
)
from backend.app.services.historical_calibration_v2_policy_service import POLICY
from backend.app.services.portal_calibration_v2_importer import canonical_json

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path, relative: str, rows: int | None = None) -> dict[str, object]:
    return {
        "relative_path": relative,
        "sha256": _sha(path),
        "byte_count": path.stat().st_size,
        "row_count": rows,
    }


def _modern_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    profile_rows: list[dict[str, object]] = []
    quality_rows: list[dict[str, object]] = []
    policy_rows: list[dict[str, object]] = []
    for index, weekday in enumerate(WEEKDAYS):
        for bucket, slowdown in (
            (825, 1.0),
            (840, 2.0 + index),
            (1125, 3.0 + index),
            (1140, 9.0),
        ):
            edge = "edge-a" if index < 3 else "edge-b"
            key = {
                "app_edge_id": edge,
                "weekday": weekday,
                "bucket_start_minute": bucket,
            }
            profile_rows.append(
                {
                    **key,
                    "direction": "northbound",
                    "slowdown_p50": slowdown - 0.5,
                    "slowdown_p85": slowdown - 0.2,
                    "slowdown_p90": slowdown - 0.1,
                    "slowdown_p95": slowdown,
                    "speed_kph_p10": 20.0,
                    "speed_kph_p50": 40.0,
                    "speed_kph_p85": 50.0,
                    "speed_kph_p90": 55.0,
                    "speed_kph_p95": 60.0,
                    "flow_vph_p50": 1000.0,
                    "flow_vph_p85": 1200.0,
                    "flow_vph_p90": 1300.0,
                    "flow_vph_p95": 1400.0,
                    "source_station_ids_json": '["station-a"]',
                    "calibration_status": "not_calibrated",
                }
            )
            quality_rows.append(
                {
                    **key,
                    "reference_speed_kph_p50": 100.0,
                    "zero_speed_date_count": 1 if weekday == "tuesday" else 0,
                    "zero_speed_fraction_present": 0.01
                    if weekday == "tuesday"
                    else 0.0,
                    "sample_count_p10": 10.0,
                    "sample_count_p50": 50.0,
                    "sample_count_p90": 100.0,
                    "detector_support_count_p50": 2.0,
                    "station_support_count_p50": 1.0,
                }
            )
            policy_rows.append(
                {
                    **key,
                    "observed_date_count": 135 if index < 3 else 100,
                    "coverage_fraction": 1.0 if index < 3 else 100 / 135,
                    "date_support_class": (
                        "direct_calibration_evidence"
                        if index < 3
                        else "insufficient_direct_evidence"
                    ),
                    "speed_missing_within_observed_date_count": 0,
                }
            )
    graph = pd.DataFrame(
        [
            {
                "edge_id": "edge-a",
                "maxspeed_kph": 100.0,
                "road_name": "Alpha Freeway",
                "road_class": "motorway",
                "ref": "I 1",
            },
            {
                "edge_id": "edge-b",
                "maxspeed_kph": 100.0,
                "road_name": "Beta Freeway",
                "road_class": "motorway",
                "ref": "I 2",
            },
        ]
    )
    return (
        pd.DataFrame(profile_rows),
        pd.DataFrame(quality_rows),
        pd.DataFrame(policy_rows),
        graph,
    )


def test_pm_filter_weekdays_support_ranking_and_sensitivities_are_exact() -> None:
    profiles, quality, policy, graph = _modern_frames()
    original_quality = quality.copy(deep=True)
    frame = _build_modern_frame(
        profiles.sample(frac=1, random_state=2),
        quality.sample(frac=1, random_state=3),
        policy.sample(frac=1, random_state=4),
        graph,
        {"station-a": "Alpha at Example"},
        reproduced_legacy_p95=4.0,
    )

    assert set(frame["weekday"]) == set(WEEKDAYS)
    assert not set(frame["weekday"]) - set(WEEKDAYS)
    assert set(frame["bucket_start_minute"]) == {PM_START_MINUTE, PM_END_MINUTE - 15}
    assert len(frame) == 10
    assert set(frame["date_support_class"]) == {
        "direct_calibration_evidence",
        "insufficient_direct_evidence",
    }
    assert {
        tuple(json.loads(value)) for value in frame["station_context_json"]
    } == {("Alpha at Example",)}
    assert frame["tail_rank"].dropna().astype(int).sort_values().tolist() == list(
        range(1, 11)
    )
    assert (
        frame[["app_edge_id", "weekday", "bucket_start_minute"]].duplicated().sum() == 0
    )

    result = _analyze_modern(frame, reproduced_legacy_p95=4.0)
    assert result["support_sensitivity"]["all_candidates"]["count"] == 10
    assert result["support_sensitivity"]["direct_only"]["count"] == 6
    assert (
        result["zero_speed_sensitivity"]["profiles_without_zero_speed_diagnostic"][
            "count"
        ]
        == 8
    )
    pd.testing.assert_frame_equal(quality, original_quality)

    reranked = _build_modern_frame(
        profiles.sample(frac=1, random_state=99),
        quality.sample(frac=1, random_state=98),
        policy.sample(frac=1, random_state=97),
        graph,
        {"station-a": "Alpha at Example"},
        reproduced_legacy_p95=4.0,
    )
    left = frame.set_index(["app_edge_id", "weekday", "bucket_start_minute"])[
        "tail_rank"
    ]
    right = reranked.set_index(["app_edge_id", "weekday", "bucket_start_minute"])[
        "tail_rank"
    ]
    pd.testing.assert_series_equal(left.sort_index(), right.sort_index())


def test_duplicate_modern_identity_is_rejected() -> None:
    profiles, quality, policy, graph = _modern_frames()
    with pytest.raises(P95TailAnalysisError, match="Duplicate"):
        _build_modern_frame(
            pd.concat([profiles, profiles.iloc[[0]]], ignore_index=True),
            quality,
            policy,
            graph,
            {},
            reproduced_legacy_p95=4.0,
        )


def _write_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    legacy = root / "legacy"
    candidate = root / "candidate"
    characterization = root / "characterization"
    policy_directory = root / "policy"
    for path in (legacy, candidate, characterization, policy_directory):
        path.mkdir(parents=True)
    (legacy / "observations").mkdir()
    (legacy / "profiles").mkdir()
    (legacy / "station-matching").mkdir()

    graph_path = root / "edges.parquet"
    graph = pd.DataFrame(
        [
            {
                "edge_id": "edge-a",
                "maxspeed_kph": 100.0,
                "road_name": "Alpha Freeway",
                "road_class": "motorway",
                "ref": "I 1",
            },
            {
                "edge_id": "edge-b",
                "maxspeed_kph": 100.0,
                "road_name": "Beta Freeway",
                "road_class": "motorway",
                "ref": "I 2",
            },
        ]
    )
    graph.to_parquet(graph_path, index=False)

    observation_rows = []
    match_rows = []
    multipliers = [1.0, 2.0, 3.0, 4.0, 5.0]
    for iso_weekday, multiplier in enumerate(multipliers, start=1):
        station = f"station-{iso_weekday}"
        edge = "edge-a" if iso_weekday < 5 else "edge-b"
        observation_rows.append(
            {
                "station_or_segment_id": station,
                "local_date": pd.Timestamp(f"2025-09-{iso_weekday + 7:02d}"),
                "local_month": 9,
                "iso_weekday": iso_weekday,
                "minute_of_day": PM_START_MINUTE + iso_weekday * 15,
                "speed_kph": 100 / multiplier,
                "volume": 10,
                "quality_flag": "good",
                "profile_eligible": True,
            }
        )
        match_rows.append(
            {
                "station_id": station,
                "edge_id": edge,
                "direction": "NORTH",
                "road_name": "Alpha Freeway" if edge == "edge-a" else "Beta Freeway",
                "road_class": "motorway",
                "edge_ref": "I 1" if edge == "edge-a" else "I 2",
                "highway_name": "Fixture Highway",
                "location_text": f"Fixture location {iso_weekday}",
                "status": "accepted",
            }
        )
    pd.DataFrame(observation_rows).to_parquet(
        legacy / "observations/fixture.parquet", index=False
    )
    pd.DataFrame(match_rows).to_parquet(
        legacy / "station-matching/station-edge-matches.parquet", index=False
    )
    legacy_profiles = legacy / "profiles/edge-bucket-profiles.parquet"
    pd.DataFrame([{"fixture": 1}]).to_parquet(legacy_profiles, index=False)
    distribution_p95 = float(np.quantile(np.asarray(multipliers), 0.95))
    legacy_report = {
        "compiler_version": "portal-background-profile-compiler-v1",
        "matcher_version": "portal-station-edge-matcher-v1",
        "graph_version": "fixture-graph",
        "source_campaign": "fixture-campaign",
        "artifact_sha256": _sha(legacy_profiles),
        "profiles": [
            {
                "period": "weekday_afternoon",
                "observation_count": 5,
                "source_window": "2025-09-08 to 2025-09-12",
                "stats": {
                    "p95_multiplier": distribution_p95,
                    "multiplier_samples": multipliers,
                },
            }
        ],
    }
    (legacy / "profiles/profile-build-report.json").write_text(
        json.dumps(legacy_report)
    )
    (legacy / "finalization-manifest.json").write_text("{}")

    profiles, quality, policy_rows, _ = _modern_frames()
    profiles = profiles.loc[profiles["bucket_start_minute"].eq(PM_START_MINUTE)].copy()
    quality = quality.loc[quality["bucket_start_minute"].eq(PM_START_MINUTE)].copy()
    policy_rows = policy_rows.loc[
        policy_rows["bucket_start_minute"].eq(PM_START_MINUTE)
    ].copy()
    profile_path = candidate / "edge-time-distributions.parquet"
    profiles.to_parquet(profile_path, index=False)
    source_manifest = HistoricalCalibrationCompilerManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_compilation",
            "artifact_id": "fixture.phase-1.3",
            "artifact_status": "historical_input",
            "calibration_status": "not_calibrated",
            "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
            "compiler": {
                "name": "historical_calibration_v2_compiler",
                "code_version": "phase-1.3-v1",
                "model_version": None,
            },
            "source_corpus_digest": "1" * 64,
            "source_integrity_digest": "2" * 64,
            "source_campaign_manifest_sha256": "3" * 64,
            "graph_version": "fixture-graph",
            "graph_edges_sha256": _sha(graph_path),
            "source_window_start": date(2025, 9, 1),
            "source_window_end": date(2025, 10, 31),
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "weekdays": WEEKDAYS,
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
                "sha256": "4" * 64,
                "byte_count": 0,
                "row_count": 5,
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
    source_manifest_path = candidate / "historical-calibration-manifest.json"
    source_manifest_path.write_text(
        canonical_json(source_manifest.model_dump(mode="json"))
    )

    quality_path = characterization / "profile-quality-characterization.parquet"
    quality.to_parquet(quality_path, index=False)
    characterization_manifest = QualityCharacterizationManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_quality_characterization",
            "evidence_status": "descriptive_characterization_only",
            "calibration_status": "not_calibrated",
            "source_profile_status": "candidate_unvalidated",
            "generated_at": datetime(2026, 1, 2, tzinfo=UTC),
            "analyzer": {
                "name": "quality",
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
            "source_phase_1_3_manifest_sha256": _sha(source_manifest_path),
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_corpus_digest": source_manifest.source_corpus_digest,
            "source_integrity_digest": source_manifest.source_integrity_digest,
            "graph_version": source_manifest.graph_version,
            "input_date_level": {
                "relative_path": "edge-day-15m.parquet",
                "sha256": "6" * 64,
                "byte_count": 0,
                "row_count": 5,
            },
            "input_weekday_profiles": _identity(
                profile_path, profile_path.name, len(profiles)
            ),
            "output_profile_characterization": _identity(
                quality_path, quality_path.name, len(quality)
            ),
            "output_json": {
                "relative_path": "quality-characterization.json",
                "sha256": "7" * 64,
                "byte_count": 0,
                "row_count": None,
            },
            "output_markdown": {
                "relative_path": "QUALITY_CHARACTERIZATION.md",
                "sha256": "8" * 64,
                "byte_count": 0,
                "row_count": None,
            },
            "content_digest": "9" * 64,
        }
    )
    characterization_manifest_path = (
        characterization / "quality-characterization-manifest.json"
    )
    characterization_manifest_path.write_text(
        canonical_json(characterization_manifest.model_dump(mode="json"))
    )

    policy_path = policy_directory / "quality-policy-profile-status.parquet"
    policy_rows.to_parquet(policy_path, index=False)
    policy_manifest = HistoricalQualityPolicyManifestV1.model_validate(
        {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_calibration_quality_policy_application",
            "artifact_status": "quality_policy_applied_unvalidated",
            "calibration_status": "not_calibrated",
            "source_profile_status": "candidate_unvalidated",
            "generated_at": datetime(2026, 1, 3, tzinfo=UTC),
            "compiler": {
                "name": "historical_calibration_v2_quality_policy",
                "code_version": "phase-1.3-policy-v1",
                "model_version": None,
            },
            "policy_version": "phase-1.3-policy-v1",
            "policy": POLICY.model_dump(mode="json"),
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_phase_1_3_manifest_sha256": _sha(source_manifest_path),
            "source_characterization_content_digest": characterization_manifest.content_digest,
            "source_characterization_manifest_sha256": _sha(
                characterization_manifest_path
            ),
            "graph_version": source_manifest.graph_version,
            "input_profile_characterization": _identity(
                quality_path, quality_path.name, len(quality)
            ),
            "input_characterization_report": {
                "relative_path": "quality-characterization.json",
                "sha256": "a" * 64,
                "byte_count": 0,
                "row_count": None,
            },
            "output_profile_status": _identity(
                policy_path, policy_path.name, len(policy_rows)
            ),
            "output_report_json": {
                "relative_path": "quality-policy-report.json",
                "sha256": "b" * 64,
                "byte_count": 0,
                "row_count": None,
            },
            "output_report_markdown": {
                "relative_path": "QUALITY_POLICY_REPORT.md",
                "sha256": "c" * 64,
                "byte_count": 0,
                "row_count": None,
            },
            "content_digest": "d" * 64,
        }
    )
    (policy_directory / "quality-policy-manifest.json").write_text(
        canonical_json(policy_manifest.model_dump(mode="json"))
    )
    return legacy, candidate, characterization, policy_directory, graph_path


def test_repeat_runs_are_deterministic_and_preserve_explicit_weighting(
    tmp_path: Path,
) -> None:
    inputs = _write_fixture(tmp_path)
    first = analyze_historical_calibration_v2_p95_tail(
        legacy_campaign_directory=inputs[0],
        candidate_directory=inputs[1],
        characterization_directory=inputs[2],
        policy_directory=inputs[3],
        graph_edges_path=inputs[4],
        output_directory=tmp_path / "out-a",
    )
    second = analyze_historical_calibration_v2_p95_tail(
        legacy_campaign_directory=inputs[0],
        candidate_directory=inputs[1],
        characterization_directory=inputs[2],
        policy_directory=inputs[3],
        graph_edges_path=inputs[4],
        output_directory=tmp_path / "out-b",
    )

    assert first.content_digest == second.content_digest
    assert first.output_profiles.sha256 == second.output_profiles.sha256
    summary = json.loads((tmp_path / "out-a/p95-tail-summary.json").read_text())
    assert summary["legacy"]["weighting"]["grain"].startswith(
        "one accepted normalized station-time"
    )
    assert summary["legacy"]["distribution"]["count"] == 5
    assert summary["modern"]["pm_profile_count"] == 5
    assert first.calibration_status == "not_calibrated"
    assert set(
        parquet.read_table(tmp_path / "out-a" / PROFILE_OUTPUT).to_pandas()["weekday"]
    ) == set(WEEKDAYS)


def test_interrupted_analysis_cannot_promote_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _write_fixture(tmp_path)
    original = parquet.write_table

    def fail_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(parquet, "write_table", fail_write)
    output = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        analyze_historical_calibration_v2_p95_tail(
            legacy_campaign_directory=inputs[0],
            candidate_directory=inputs[1],
            characterization_directory=inputs[2],
            policy_directory=inputs[3],
            graph_edges_path=inputs[4],
            output_directory=output,
        )
    monkeypatch.setattr(parquet, "write_table", original)
    assert not output.exists()
    assert not (output / MANIFEST_OUTPUT).exists()
