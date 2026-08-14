"""Phase 2.2e optimized regional station observer tests."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from backend.app.schemas.simulation import SimulationRunRequest
from backend.app.services.sumo.station_crossing_counter_service import (
    StationDefinition,
    observe_crossings,
)
from backend.app.services.sumo.station_telemetry_service import (
    OptimizedStationObserver,
    ProductionStationDefinition,
    StationTelemetryError,
    build_station_observation_plan,
    observe_production_crossings,
    summarize_plan,
)

POLICY_DIGEST = "5a5506a26997be4fbebbec8a11569e62a13744ddb13e3ec9e861dc356a349ef2"


def test_request_is_optional_and_policy_bound() -> None:
    request = SimulationRunRequest()
    assert request.station_telemetry_interval_seconds is None
    assert request.station_cross_section_policy_digest is None
    enabled = SimulationRunRequest(
        station_telemetry_interval_seconds=900,
        station_cross_section_policy_digest=POLICY_DIGEST,
    )
    assert enabled.station_telemetry_interval_seconds == 900
    with pytest.raises(ValidationError):
        SimulationRunRequest(station_telemetry_interval_seconds=900)


def test_production_plan_loads_only_356_accepted_stations() -> None:
    policy = Path("data/sumo/networks/active/station-cross-section-policy-v1")
    network = Path("data/sumo/networks/active/metro.net.xml")
    plan = build_station_observation_plan(
        policy_directory=policy,
        expected_policy_digest=POLICY_DIGEST,
        network_path=network,
    )
    summary = summarize_plan(plan)
    assert summary["accepted_station_count"] == 356
    assert len({row["station_id"] for row in plan["stations"]}) == 356
    source = pq.read_table(policy / "station-cross-section-policy.parquet").to_pylist()
    excluded = {
        row["station_id"] for row in source if not row["flow_measurement_eligible"]
    }
    assert excluded.isdisjoint({row["station_id"] for row in plan["stations"]})


def test_optimized_events_equal_phase_2_2c_oracle_for_within_edge() -> None:
    station = ProductionStationDefinition(
        "a", "app", "within_edge_position", "edge", 300, None, None, "NORTH", "r", "p"
    )
    previous = {"v": _observation("edge", 250, 1, 9)}
    current = {"v": _observation("edge", 350, 1, 10)}
    optimized = observe_production_crossings(
        previous=previous,
        current=current,
        departed_ids=set(),
        departure_positions_m={},
        stations=[station],
        emitted=set(),
    )
    oracle = observe_crossings(
        previous={"v": {**previous["v"], "speed_mps": 0}},
        current={"v": {**current["v"], "speed_mps": 0}},
        departed_ids=set(),
        departure_positions_m={},
        stations=[StationDefinition("a", "edge", 300)],
        emitted=set(),
    )
    assert [event["event_id"] for event in optimized] == [event["event_id"] for event in oracle]


def test_transition_direct_departure_multiple_stations_and_route_occurrence() -> None:
    stations = [
        _station("a", 300),
        _station("b", 700),
        ProductionStationDefinition(
            "t", "app", "edge_transition", None, None, "before", "edge", "NORTH", "r", "pt"
        ),
    ]
    emitted: set[str] = set()
    events = observe_production_crossings(
        previous={"through": _observation("before", 90, 0, 9)},
        current={
            "through": _observation("edge", 800, 1, 10),
            "direct": _observation("edge", 800, 0, 10),
            "down": _observation("edge", 850, 0, 10),
        },
        departed_ids={"direct", "down"},
        departure_positions_m={"direct": 100, "down": 800},
        stations=stations,
        emitted=emitted,
    )
    assert {(event["vehicle_id"], event["station_id"]) for event in events} == {
        ("through", "a"), ("through", "b"), ("through", "t"),
        ("direct", "a"), ("direct", "b"),
    }
    repeated = observe_production_crossings(
        previous={"through": _observation("edge", 200, 2, 20)},
        current={"through": _observation("edge", 800, 2, 21)},
        departed_ids=set(), departure_positions_m={}, stations=stations, emitted=emitted,
    )
    assert {event["station_id"] for event in repeated} == {"a", "b"}


def test_checkpoint_resume_exactly_once_dense_900_rows_and_variants() -> None:
    plan = _plan([_station("a", 300), _station("b", 700)])
    first = OptimizedStationObserver(plan, variant="baseline")
    first.observe(
        time_seconds=100,
        subscription_rows={"v": _observation("edge", 250, 1, 100)},
        departed_ids=set(), arrived_ids=set(), departure_positions_m={},
    )
    payload = json.loads(json.dumps(first.checkpoint_state(), sort_keys=True))
    resumed = OptimizedStationObserver(plan, variant="baseline", restored_state=payload)
    events, _ = resumed.observe(
        time_seconds=101,
        subscription_rows={"v": _observation("edge", 800, 1, 101)},
        departed_ids=set(), arrived_ids=set(), departure_positions_m={},
    )
    assert [event["station_id"] for event in events] == ["a", "b"]
    assert resumed.observe(
        time_seconds=102,
        subscription_rows={"v": _observation("edge", 850, 1, 102)},
        departed_ids=set(), arrived_ids=set(), departure_positions_m={},
    )[0] == []
    _, rows = resumed.observe(
        time_seconds=900,
        subscription_rows={}, departed_ids=set(), arrived_ids={"v"}, departure_positions_m={},
    )
    assert len(rows) == 2
    assert [row["crossing_count"] for row in rows] == [1, 1]
    assert all(row["flow_vph"] == 4 for row in rows)
    scenario = OptimizedStationObserver(plan, variant="scenario")
    _, zero_rows = scenario.observe(
        time_seconds=900, subscription_rows={}, departed_ids=set(), arrived_ids=set(),
        departure_positions_m={},
    )
    assert {row["variant"] for row in zero_rows} == {"scenario"}
    assert all(row["crossing_count"] == 0 for row in zero_rows)


def test_checkpoint_state_is_deterministic_bounded_and_plan_identity_checked() -> None:
    plan = _plan([_station("b", 700), _station("a", 300)])
    first = OptimizedStationObserver(plan, variant="baseline")
    second = OptimizedStationObserver(plan, variant="baseline")
    assert json.dumps(first.checkpoint_state(), sort_keys=True) == json.dumps(
        second.checkpoint_state(), sort_keys=True
    )
    bad = first.checkpoint_state()
    bad["observation_plan_digest"] = "0" * 64
    with pytest.raises(StationTelemetryError, match="incompatible"):
        OptimizedStationObserver(plan, variant="baseline", restored_state=bad)


def _station(station_id: str, position: float) -> ProductionStationDefinition:
    return ProductionStationDefinition(
        station_id, "app", "within_edge_position", "edge", position,
        None, None, "NORTH", "r", f"p-{station_id}"
    )


def _observation(edge: str, position: float, route_index: int, time: int) -> dict[str, object]:
    return {
        "road_id": edge,
        "lane_position_m": position,
        "route_index": route_index,
        "time_seconds": time,
    }


def _plan(stations: list[ProductionStationDefinition]) -> dict[str, object]:
    rows = [station.__dict__ for station in stations]
    return {
        "stations": rows,
        "relevant_edge_ids": ["before", "edge"],
        "observation_plan_digest": "a" * 64,
        "policy_content_digest": POLICY_DIGEST,
    }
