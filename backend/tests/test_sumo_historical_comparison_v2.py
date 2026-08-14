"""Focused Phase 2.2g station-cross-section Comparator-v2 tests."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from backend.app.services.sumo.historical_comparison_v2_service import (
    SumoHistoricalComparisonV2Error,
    aggregate_station_profiles,
    build_app_edge_comparison_frame,
    build_scoreboard,
    build_station_comparison_frame,
    calculate_flow_metrics,
    compare_roundtrip_rows,
    content_digest,
    frame_digest,
    promote_completed_pending,
    reconstruct_app_edge_date_flow,
    reconstruct_app_edge_flow_profiles,
    resolve_local_interval,
)


def test_local_timezone_weekday_and_dst_mapping_are_pacific() -> None:
    assert resolve_local_interval(
        datetime.fromisoformat("2026-09-15T00:00:00+00:00"), 0
    ) == ("monday", 1020, "2026-09-14")
    assert resolve_local_interval(
        datetime.fromisoformat("2026-03-09T07:00:00+00:00"), 0
    ) == ("monday", 0, "2026-03-09")
    with pytest.raises(ValueError, match="weekend"):
        resolve_local_interval(datetime.fromisoformat("2026-09-19T07:00:00+00:00"), 0)


def test_station_profile_preserves_equal_date_distribution_and_missing_is_not_zero() -> (
    None
):
    dates = pd.DataFrame(
        [
            _station_date("2026-09-07", 40.0, detectors=2),
            _station_date("2026-09-14", 80.0, detectors=3),
        ]
    )
    profile = aggregate_station_profiles(
        dates,
        source_window_start=date(2026, 9, 7),
        source_window_end=date(2026, 9, 14),
        source_corpus_digest="a" * 64,
    ).iloc[0]
    assert profile.flow_vph_p50 == 60.0
    assert profile.observed_date_count == 2
    assert profile.possible_date_count == 2
    assert profile.detector_support_count_min == 2
    assert profile.date_support_class == "direct_calibration_evidence"
    assert len(dates) == 2  # no missing date was synthesized as zero


def test_phase_one_flow_roundtrip_reconstructs_date_median_then_weekday_distribution() -> (
    None
):
    matrix = (
        ("2026-09-07", (100.0, 100.0, 0.0)),
        ("2026-09-14", (0.0, 100.0, 100.0)),
        ("2026-09-21", (0.0, 0.0, 0.0)),
    )
    rows = []
    for day, values in matrix:
        for station, flow in zip(("a", "b", "c"), values, strict=True):
            row = _station_date(day, flow, detectors=1)
            row["station_id"] = f"portal-station-{station}"
            rows.append(row)
    station_dates = pd.DataFrame(rows)
    edge_dates = reconstruct_app_edge_date_flow(station_dates)
    profiles = reconstruct_app_edge_flow_profiles(edge_dates)
    assert list(edge_dates["flow_vph"]) == [100.0, 100.0, 0.0]
    assert profiles.iloc[0].flow_vph_p50 == 100.0
    station_profiles = aggregate_station_profiles(
        station_dates,
        source_window_start=date(2026, 9, 7),
        source_window_end=date(2026, 9, 21),
        source_corpus_digest="a" * 64,
    )
    assert station_profiles.flow_vph_p50.median() == 0.0
    comparison = compare_roundtrip_rows(
        edge_dates,
        edge_dates.copy(),
        identity=[
            "app_edge_id",
            "direction",
            "local_date",
            "weekday",
            "bucket_start_minute",
        ],
        numeric_fields=["volume_count", "flow_vph"],
        exact_fields=[
            "station_count",
            "detector_count",
            "sample_count",
            "source_station_ids_json",
        ],
    )
    assert comparison["mismatch_row_count"] == 0
    assert comparison["exact_or_numerically_equivalent_match_count"] == 3


def test_roundtrip_reports_numeric_mismatch_and_maximum_difference() -> None:
    reconstructed = reconstruct_app_edge_date_flow(
        pd.DataFrame([_station_date("2026-09-07", 40.0, detectors=1)])
    )
    accepted = reconstructed.copy()
    accepted.loc[0, "flow_vph"] = 44.0
    result = compare_roundtrip_rows(
        reconstructed,
        accepted,
        identity=[
            "app_edge_id",
            "direction",
            "local_date",
            "weekday",
            "bucket_start_minute",
        ],
        numeric_fields=["volume_count", "flow_vph"],
        exact_fields=[
            "station_count",
            "detector_count",
            "sample_count",
            "source_station_ids_json",
        ],
    )
    assert result["mismatch_row_count"] == 1
    assert result["maximum_numeric_difference"]["flow_vph"] == 4.0
    assert result["mismatch_reason_counts"] == {"numeric_mismatch:flow_vph": 1}


def test_roundtrip_reports_missing_rows_on_either_side() -> None:
    reconstructed = reconstruct_app_edge_date_flow(
        pd.DataFrame([_station_date("2026-09-07", 40.0, detectors=1)])
    )
    accepted = reconstructed.copy()
    accepted.loc[0, "app_edge_id"] = "accepted-only"
    result = compare_roundtrip_rows(
        reconstructed,
        accepted,
        identity=[
            "app_edge_id",
            "direction",
            "local_date",
            "weekday",
            "bucket_start_minute",
        ],
        numeric_fields=["volume_count", "flow_vph"],
        exact_fields=[
            "station_count",
            "detector_count",
            "sample_count",
            "source_station_ids_json",
        ],
    )
    assert result["mismatch_row_count"] == 2
    assert result["missing_from_reconstructed_count"] == 1
    assert result["missing_from_accepted_count"] == 1
    assert result["mismatch_reason_counts"] == {
        "missing_from_accepted": 1,
        "missing_from_reconstructed": 1,
    }


def test_station_sumo_crossing_flow_and_resolved_departure_are_primary_eligible() -> (
    None
):
    frame = _station_comparisons()
    baseline_a = frame.loc[
        frame["variant"].eq("baseline") & frame["station_id"].eq("portal-station-a")
    ].iloc[0]
    assert baseline_a.sumo_flow_vph == 160.0
    assert baseline_a.resolved_direct_departure_count == 1
    assert baseline_a.flow_evidence_eligible
    assert baseline_a.primary_flow_scoring


def test_incomplete_and_unresolved_sumo_rows_are_retained_but_excluded() -> None:
    telemetry = _telemetry()
    telemetry.loc[
        telemetry["station_id"].eq("portal-station-b"),
        ["flow_measurement_complete", "unresolved_direct_departure_count"],
    ] = [False, 1]
    frame = _station_comparisons(telemetry=telemetry)
    rows = frame.loc[frame["station_id"].eq("portal-station-b")]
    assert len(rows) == 2
    assert not rows["flow_evidence_eligible"].any()
    reasons = set().union(
        *(
            set(__import__("json").loads(value))
            for value in rows["primary_exclusion_reasons_json"]
        )
    )
    assert {"sumo_flow_incomplete", "unresolved_direct_departure"} <= reasons


def test_review_mapping_is_reported_and_never_primary() -> None:
    frame = _station_comparisons()
    review = frame.loc[frame["station_id"].eq("portal-station-c")]
    assert len(review) == 2
    assert not review["flow_evidence_eligible"].any()
    assert set(review["primary_exclusion_reason"]) == {"station_mapping_not_accepted"}


def test_all_contributing_stations_required_and_partial_median_not_primary() -> None:
    stations = _station_comparisons()
    stations.loc[
        stations["station_id"].eq("portal-station-b"), "flow_evidence_eligible"
    ] = False
    edges = _edge_comparisons(stations=stations)
    app1 = edges.loc[edges["app_edge_id"].eq("app-1")]
    assert not app1["flow_evidence_eligible"].any()
    assert app1["sumo_flow_vph"].isna().all()
    assert app1["diagnostic_partial_sumo_flow_vph"].notna().all()


def test_complete_app_edge_uses_median_of_same_station_identities() -> None:
    edges = _edge_comparisons()
    baseline = edges.loc[
        edges["variant"].eq("baseline") & edges["app_edge_id"].eq("app-1")
    ].iloc[0]
    assert baseline.required_station_count == 2
    assert baseline.sumo_flow_vph == 120.0  # median of station A=160 and B=80
    assert baseline.historical_flow_vph == 100.0  # authoritative Phase 1 edge P50
    assert baseline.signed_error_vph == 20.0


def test_baseline_and_scenario_are_separate_and_speed_never_primary() -> None:
    stations = _station_comparisons()
    edges = _edge_comparisons(stations=stations)
    assert set(stations["variant"]) == {"baseline", "scenario"}
    assert not stations.loc[
        stations["variant"].eq("scenario"), "primary_flow_scoring"
    ].any()
    assert not stations["point_speed_primary_eligible"].any()
    assert not edges["point_speed_primary_eligible"].any()


def test_zero_historical_flow_keeps_absolute_error_but_percentage_is_undefined() -> (
    None
):
    frame = pd.DataFrame(
        [
            {
                "primary_flow_scoring": True,
                "flow_evidence_eligible": True,
                "signed_error_vph": 12.0,
                "historical_flow_vph": 0.0,
                "flow_ratio": None,
            }
        ]
    )
    metrics = calculate_flow_metrics(frame)
    assert metrics["flow_wape"] is None
    assert metrics["flow_wape_undefined_reason"] == "historical_denominator_is_zero"
    assert metrics["flow_mae_vph"] == 12.0


def test_wape_is_ratio_of_sums_not_mean_ape_and_bias_sign_is_sumo_minus_portal() -> (
    None
):
    frame = pd.DataFrame(
        [
            {
                "primary_flow_scoring": True,
                "flow_evidence_eligible": True,
                "signed_error_vph": -50.0,
                "historical_flow_vph": 100.0,
                "flow_ratio": 0.5,
            },
            {
                "primary_flow_scoring": True,
                "flow_evidence_eligible": True,
                "signed_error_vph": 50.0,
                "historical_flow_vph": 900.0,
                "flow_ratio": 950 / 900,
            },
        ]
    )
    metrics = calculate_flow_metrics(frame)
    assert metrics["flow_wape"] == pytest.approx(100 / 1000)
    assert metrics["flow_signed_bias_vph"] == 0.0


def test_scoreboard_breakdowns_and_integrity_are_deterministic() -> None:
    stations = _station_comparisons()
    edges = _edge_comparisons(stations=stations)
    first = build_scoreboard(
        stations=stations, app_edges=edges, comparator_v1_summary=None
    )
    second = build_scoreboard(
        stations=stations.sample(frac=1, random_state=2),
        app_edges=edges.sample(frac=1, random_state=3),
        comparator_v1_summary=None,
    )
    assert first == second
    assert not any(first["integrity"].values())
    assert first["breakdowns"]["corridor"]


def test_repeat_order_produces_same_row_and_manifest_identity() -> None:
    frame = _station_comparisons()
    assert frame_digest(frame) == frame_digest(
        frame.sort_values(["variant", "interval_start_seconds", "station_id"])
    )
    assert content_digest({"generated_at": "one", "value": 1}) == content_digest(
        {"generated_at": "two", "value": 1}
    )


def test_partial_output_cannot_promote(tmp_path: Path) -> None:
    pending = tmp_path / ".pending"
    pending.mkdir()
    (pending / "station-comparisons.parquet").write_bytes(b"partial")
    with pytest.raises(SumoHistoricalComparisonV2Error, match="no manifest"):
        promote_completed_pending(pending, tmp_path / "final")
    assert pending.exists()
    assert not (tmp_path / "final").exists()


def _station_date(day: str, flow: float, *, detectors: int) -> dict[str, object]:
    return {
        "station_id": "a",
        "app_edge_id": "app-1",
        "direction": "northbound",
        "local_date": date.fromisoformat(day),
        "weekday": "monday",
        "bucket_start_minute": 1020,
        "volume_count": flow / 4,
        "flow_vph": flow,
        "detector_count": detectors,
        "sample_count": 10,
    }


def _station_profiles() -> pd.DataFrame:
    rows = []
    for station, edge, target in (
        ("a", "app-1", 100.0),
        ("b", "app-1", 120.0),
        ("c", "app-2", 50.0),
    ):
        rows.append(
            {
                "record_id": f"profile-{station}",
                "station_id": f"portal-station-{station}",
                "app_edge_id": edge,
                "direction": "northbound",
                "weekday": "monday",
                "bucket_start_minute": 1020,
                "possible_date_count": 135,
                "observed_date_count": 130,
                "coverage_fraction": 130 / 135,
                "date_support_class": "direct_calibration_evidence",
                "flow_direct_calibration_eligible": True,
                "flow_vph_p10": target * 0.8,
                "flow_vph_p50": target,
                "flow_vph_p90": target * 1.2,
                "detector_support_count_p50": 2.0,
                "sample_count_p50": 100.0,
            }
        )
    return pd.DataFrame(rows)


def _edge_profiles() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "record_id": "edge-profile-1",
                "app_edge_id": "app-1",
                "direction": "northbound",
                "weekday": "monday",
                "bucket_start_minute": 1020,
                "flow_vph_mean": 105.0,
                "flow_vph_p50": 100.0,
                "flow_vph_p85": 130.0,
                "flow_vph_p90": 140.0,
                "flow_vph_p95": 150.0,
                "source_station_ids_json": '["a","b"]',
            },
            {
                "record_id": "edge-profile-2",
                "app_edge_id": "app-2",
                "direction": "northbound",
                "weekday": "monday",
                "bucket_start_minute": 1020,
                "flow_vph_mean": 50.0,
                "flow_vph_p50": 50.0,
                "flow_vph_p85": 60.0,
                "flow_vph_p90": 65.0,
                "flow_vph_p95": 70.0,
                "source_station_ids_json": '["c"]',
            },
        ]
    )


def _quality_policy() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "app_edge_id": edge,
                "weekday": "monday",
                "bucket_start_minute": 1020,
                "date_support_class": "direct_calibration_evidence",
                "flow_direct_calibration_eligible": True,
                "observed_date_count": 130,
                "possible_date_count": 135,
                "coverage_fraction": 130 / 135,
            }
            for edge in ("app-1", "app-2")
        ]
    )


def _station_policy() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "station_id": "portal-station-a",
                "app_edge_id": "app-1",
                "direction": "NORTH",
                "flow_measurement_eligible": True,
                "policy_state": "accepted_within_edge",
                "cross_section_type": "within_edge_position",
                "road_ref": "I 5",
                "road_name": "I-5",
            },
            {
                "station_id": "portal-station-b",
                "app_edge_id": "app-1",
                "direction": "NORTH",
                "flow_measurement_eligible": True,
                "policy_state": "accepted_edge_transition",
                "cross_section_type": "edge_transition",
                "road_ref": "I 5",
                "road_name": "I-5",
            },
            {
                "station_id": "portal-station-c",
                "app_edge_id": "app-2",
                "direction": "NORTH",
                "flow_measurement_eligible": False,
                "policy_state": "review_projection_distance",
                "cross_section_type": None,
                "road_ref": "I 205",
                "road_name": "I-205",
            },
        ]
    )


def _telemetry() -> pd.DataFrame:
    rows = []
    for variant in ("baseline", "scenario"):
        for station, edge, count in (("a", "app-1", 40), ("b", "app-1", 20)):
            rows.append(
                {
                    "variant": variant,
                    "station_id": f"portal-station-{station}",
                    "app_edge_id": edge,
                    "interval_start_seconds": 0,
                    "interval_end_seconds": 900,
                    "interval_duration_seconds": 900,
                    "crossing_count": count,
                    "flow_vph": count * 4.0,
                    "contributing_vehicle_count": count,
                    "resolved_direct_departure_count": 1 if station == "a" else 0,
                    "unresolved_direct_departure_count": 0,
                    "flow_measurement_complete": True,
                    "policy_content_digest": "p" * 64,
                    "observation_plan_digest": "o" * 64,
                    "run_identity": "run",
                    "network_version": "network",
                    "sumo_version": "1.27.1",
                    "evidence_level": "modeled_uncalibrated",
                    "calibration_status": "not_calibrated",
                }
            )
    return pd.DataFrame(rows)


def _intervals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "interval_start_seconds": 0,
                "interval_end_seconds": 900,
                "weekday": "monday",
                "bucket_start_minute": 1020,
                "simulation_local_date": "2026-09-14",
            }
        ]
    )


def _station_comparisons(telemetry: pd.DataFrame | None = None) -> pd.DataFrame:
    return build_station_comparison_frame(
        station_profiles=_station_profiles(),
        edge_profiles=_edge_profiles(),
        quality_policy=_quality_policy(),
        station_policy=_station_policy(),
        telemetry=_telemetry() if telemetry is None else telemetry,
        intervals=_intervals(),
        expected_policy_digest="p" * 64,
        expected_observation_plan_digest="o" * 64,
    )


def _edge_comparisons(stations: pd.DataFrame | None = None) -> pd.DataFrame:
    return build_app_edge_comparison_frame(
        station_comparisons=_station_comparisons() if stations is None else stations,
        edge_profiles=_edge_profiles(),
        quality_policy=_quality_policy(),
        intervals=_intervals(),
        variants=["baseline", "scenario"],
        graph_edges=pd.DataFrame(
            [
                {
                    "edge_id": "app-1",
                    "ref": "I 5",
                    "road_name": "I-5",
                    "road_class": "motorway",
                },
                {
                    "edge_id": "app-2",
                    "ref": "I 205",
                    "road_name": "I-205",
                    "road_class": "motorway",
                },
            ]
        ),
    )
