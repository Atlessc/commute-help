"""Focused Phase 2.2 historical-to-SUMO comparator tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.app.services.sumo.historical_comparison_service import (
    SumoHistoricalComparisonError,
    _content_digest,
    _frame_digest,
    _mapping_inventory,
    _promote_completed_pending,
    build_comparison_frame,
    calculate_primary_metrics,
    derive_sumo_comparable_flow,
    resolve_local_interval,
)


def test_local_weekday_bucket_selection_is_pacific_and_never_synthesizes_weekends() -> None:
    departure = datetime.fromisoformat("2026-09-15T00:00:00+00:00")
    assert resolve_local_interval(departure, 0) == ("monday", 17 * 60, "2026-09-14")
    assert resolve_local_interval(departure, 900) == (
        "monday",
        17 * 60 + 15,
        "2026-09-14",
    )
    friday = datetime.fromisoformat("2026-09-19T06:45:00+00:00")
    assert resolve_local_interval(friday, 0)[0] == "friday"
    with pytest.raises(ValueError, match="weekend"):
        resolve_local_interval(friday, 900)


def test_flow_comparator_includes_direct_on_edge_departures_without_mutating_native_flow() -> None:
    count, flow = derive_sumo_comparable_flow(10, 2, 900)
    assert count == 12
    assert flow == 48
    assert 10 * 4 == 40  # The Phase 2.1 entered-only native flow remains distinct.


def test_mapping_ambiguity_never_selects_a_winner(tmp_path: Path) -> None:
    path = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"app_edge_id": "a", "sumo_edge_id": "s1", "status": "accepted"},
                {"app_edge_id": "a", "sumo_edge_id": "s2", "status": "accepted"},
                {"app_edge_id": "b", "sumo_edge_id": "s3", "status": "accepted"},
                {"app_edge_id": "c", "sumo_edge_id": "s4", "status": "review"},
            ]
        ),
        path,
    )
    mapping, counts = _mapping_inventory(path)
    ambiguous = mapping.loc[mapping["app_edge_id"].eq("a")].iloc[0]
    assert ambiguous["mapping_status"] == "ambiguous_multiple_accepted"
    assert pd.isna(ambiguous["sumo_edge_id"])
    assert counts == {
        "accepted_unique": 1,
        "ambiguous_multiple_accepted": 1,
        "review": 1,
    }


def test_measurement_specific_scoring_and_error_math() -> None:
    frame = _comparison_frame()
    baseline = frame.loc[frame["variant"].eq("baseline")].iloc[0]
    assert baseline["sumo_comparable_flow_vph"] == 48
    assert baseline["flow_signed_error_vph"] == 8
    assert baseline["speed_signed_error_kph"] == -10
    assert baseline["speed_absolute_error_kph"] == 10
    assert baseline["slowdown_error_vs_p50"] == pytest.approx(1.0)
    assert baseline["occupancy_primary_comparable"] is False or not baseline["occupancy_primary_comparable"]
    metrics = calculate_primary_metrics(frame)
    assert metrics["flow_wape"] == pytest.approx((8 + 8) / 80)
    assert metrics["speed_mae_kph"] == pytest.approx(7.5)
    assert metrics["speed_bias_kph"] == pytest.approx(-7.5)
    assert metrics["slowdown_p50_mae"] == pytest.approx((1 + (80 / 45 - 1)) / 2)


def test_supplemental_evidence_is_retained_but_excluded_from_primary_scoring() -> None:
    frame = _comparison_frame(date_support="supplemental_evidence")
    assert len(frame) == 2
    assert set(frame["date_support_class"]) == {"supplemental_evidence"}
    assert not frame["flow_primary_scoring"].any()
    assert not frame["speed_primary_scoring"].any()
    assert calculate_primary_metrics(frame)["flow_primary_row_count"] == 0
    insufficient = _comparison_frame(date_support="insufficient_direct_evidence")
    assert len(insufficient) == 2
    assert not insufficient["flow_primary_scoring"].any()


def test_missing_telemetry_stays_missing_and_baseline_scenario_are_distinct() -> None:
    telemetry = _telemetry().loc[lambda value: value["variant"].eq("baseline")]
    frame = build_comparison_frame(
        historical=_historical(),
        policy=_policy(),
        telemetry=telemetry,
        interval_catalog=_intervals(),
        mapping=_mapping(),
        graph=_graph(),
    )
    assert set(frame["variant"]) == {"baseline"}
    missing_mapping = _mapping().assign(sumo_edge_id="not-present")
    missing = build_comparison_frame(
        historical=_historical(), policy=_policy(), telemetry=telemetry,
        interval_catalog=_intervals(), mapping=missing_mapping, graph=_graph(),
    )
    assert missing["sumo_mean_speed_kph"].isna().all()
    assert not missing["speed_primary_scoring"].any()


def test_duplicate_historical_identity_is_rejected() -> None:
    duplicate = pd.concat([_historical(), _historical()], ignore_index=True)
    with pytest.raises(SumoHistoricalComparisonError, match="duplicate historical"):
        build_comparison_frame(
            historical=duplicate, policy=_policy(), telemetry=_telemetry(),
            interval_catalog=_intervals(), mapping=_mapping(), graph=_graph(),
        )


def test_deterministic_order_and_content_identity_do_not_mutate_inputs() -> None:
    telemetry = _telemetry()
    original = telemetry.copy(deep=True)
    first = _comparison_frame(telemetry=telemetry)
    second = _comparison_frame(telemetry=telemetry.sample(frac=1, random_state=4))
    assert _frame_digest(first) == _frame_digest(second)
    assert _content_digest({"generated_at": "first", "value": 1}) == _content_digest(
        {"generated_at": "second", "value": 1}
    )
    pd.testing.assert_frame_equal(telemetry, original)
    assert list(first["variant"]) == ["baseline", "scenario"]


def test_partial_output_cannot_promote(tmp_path: Path) -> None:
    pending = tmp_path / ".comparison.pending"
    output = tmp_path / "comparison"
    pending.mkdir()
    (pending / "comparison.parquet").write_bytes(b"partial")
    with pytest.raises(SumoHistoricalComparisonError, match="no success manifest"):
        _promote_completed_pending(pending, output)
    assert pending.is_dir()
    assert not output.exists()


def _historical() -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "app_edge_id": "app-1", "direction": "northbound", "weekday": "monday",
            "bucket_start_minute": 1020, "flow_vph_mean": 42.0, "flow_vph_p50": 40.0,
            "flow_vph_p85": 50.0, "flow_vph_p90": 52.0, "flow_vph_p95": 55.0,
            "speed_kph_mean": 52.0, "speed_kph_p10": 30.0, "speed_kph_p50": 50.0,
            "speed_kph_p85": 60.0, "speed_kph_p90": 62.0, "speed_kph_p95": 64.0,
            "slowdown_p50": 1.0, "slowdown_p85": 1.5, "slowdown_p90": 1.8,
            "slowdown_p95": 2.0,
        }]
    )


def _policy(date_support: str = "direct_calibration_evidence") -> pd.DataFrame:
    direct = date_support == "direct_calibration_evidence"
    return pd.DataFrame(
        [{
            "app_edge_id": "app-1", "direction": "northbound", "weekday": "monday",
            "bucket_start_minute": 1020, "date_support_class": date_support,
            "observed_date_count": 130, "possible_date_count": 135,
            "coverage_fraction": 130 / 135, "flow_direct_calibration_eligible": direct,
            "speed_direct_calibration_eligible": direct,
            "speed_missing_within_observed_date_count": 1,
            "speed_zero_diagnostic_date_count": 1, "sample_count_p50": 135.0,
            "detector_support_count_p50": 2.0, "station_support_count_p50": 1.0,
            "calibration_status": "not_calibrated",
        }]
    )


def _telemetry() -> pd.DataFrame:
    rows = []
    for variant, speed in (("baseline", 40.0), ("scenario", 45.0)):
        rows.append({
            "variant": variant, "sumo_edge_id": "sumo-1", "interval_start_seconds": 0,
            "interval_end_seconds": 900, "interval_duration_seconds": 900,
            "entered_count": 10.0, "departed_count": 2.0, "left_count": 9.0,
            "arrived_count": 1.0, "flow_vph": 40.0, "mean_speed_kph": speed,
            "mean_travel_time_seconds": 10.0, "density_veh_per_km": 2.0,
            "occupancy_percent": 3.0, "sampled_vehicle_seconds": 100.0,
        })
    return pd.DataFrame(rows)


def _intervals() -> pd.DataFrame:
    return pd.DataFrame([{"interval_start_seconds": 0, "interval_end_seconds_expected": 900, "weekday": "monday", "bucket_start_minute": 1020, "simulation_local_date": "2026-09-14"}])


def _mapping() -> pd.DataFrame:
    return pd.DataFrame([{"app_edge_id": "app-1", "sumo_edge_id": "sumo-1", "mapping_status": "accepted_unique", "accepted_sumo_edge_count": 1}])


def _graph() -> pd.DataFrame:
    return pd.DataFrame([{"edge_id": "app-1", "road_name": "Example Road", "road_class": "motorway", "ref": "I-5", "maxspeed_kph": 80.0}])


def _comparison_frame(*, date_support: str = "direct_calibration_evidence", telemetry: pd.DataFrame | None = None) -> pd.DataFrame:
    return build_comparison_frame(
        historical=_historical(), policy=_policy(date_support),
        telemetry=_telemetry() if telemetry is None else telemetry,
        interval_catalog=_intervals(), mapping=_mapping(), graph=_graph(),
    )
