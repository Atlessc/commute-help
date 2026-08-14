"""Phase 2.2c crossing logic and subprocess-isolated SUMO feasibility tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.services.sumo.station_crossing_counter_service import (
    StationCrossingError,
    StationDefinition,
    aggregate_station_interval,
    build_feasibility_artifact,
    observe_crossings,
)

STATIONS = [
    StationDefinition("a", "edge", 300),
    StationDefinition("b", "edge", 700),
]


def test_single_crossing_between_steps_and_no_crossing() -> None:
    emitted: set[str] = set()
    events = observe_crossings(
        previous={"v": _obs("edge", 250, route_index=1)},
        current={"v": _obs("edge", 350, route_index=1, time=10)},
        departed_ids=set(),
        departure_positions_m={},
        stations=STATIONS,
        emitted=emitted,
    )
    assert [(event["vehicle_id"], event["station_id"]) for event in events] == [
        ("v", "a")
    ]
    assert events[0]["interpolation_fraction"] == pytest.approx(0.5)
    assert (
        observe_crossings(
            previous={"v": _obs("edge", 350, route_index=1)},
            current={"v": _obs("edge", 400, route_index=1)},
            departed_ids=set(),
            departure_positions_m={},
            stations=STATIONS,
            emitted=emitted,
        )
        == []
    )


def test_edge_transition_and_direct_departure_semantics() -> None:
    transition = observe_crossings(
        previous={"v": _obs("before", 90, route_index=0)},
        current={"v": _obs("edge", 350, route_index=1)},
        departed_ids=set(),
        departure_positions_m={},
        stations=STATIONS,
        emitted=set(),
    )
    assert [event["station_id"] for event in transition] == ["a"]
    events = observe_crossings(
        previous={},
        current={
            "up": _obs("edge", 750, route_index=0),
            "exact": _obs("edge", 750, route_index=0),
            "down": _obs("edge", 850, route_index=0),
            "unknown": _obs("edge", 850, route_index=0),
        },
        departed_ids={"up", "exact", "down", "unknown"},
        departure_positions_m={"up": 100, "exact": 700, "down": 800},
        stations=STATIONS,
        emitted=set(),
    )
    assert [(event["vehicle_id"], event["station_id"]) for event in events] == [
        ("exact", "b"),
        ("up", "a"),
        ("up", "b"),
    ]


def test_two_stations_lanes_traversals_and_arrival_before_station() -> None:
    emitted: set[str] = set()
    first = observe_crossings(
        previous={"v": _obs("edge", 200, route_index=1, lane=0)},
        current={"v": _obs("edge", 800, route_index=1, lane=1)},
        departed_ids=set(),
        departure_positions_m={},
        stations=STATIONS,
        emitted=emitted,
    )
    assert [event["station_id"] for event in first] == ["a", "b"]
    assert len({event["event_id"] for event in first}) == 2
    repeated = observe_crossings(
        previous={"v": _obs("edge", 200, route_index=3)},
        current={"v": _obs("edge", 800, route_index=3)},
        departed_ids=set(),
        departure_positions_m={},
        stations=STATIONS,
        emitted=emitted,
    )
    assert len(repeated) == 2
    # A vehicle removed upstream has no current state and cannot manufacture a crossing.
    assert (
        observe_crossings(
            previous={"arrived": _obs("edge", 600)},
            current={},
            departed_ids=set(),
            departure_positions_m={},
            stations=STATIONS,
            emitted=set(),
        )
        == []
    )


def test_complete_900_second_aggregation_and_incomplete_rejection() -> None:
    rows = aggregate_station_interval(
        [
            {
                "station_id": "a",
                "vehicle_id": "v1",
                "observation_time_seconds": 10,
                "crossing_step_speed_mps": 10,
                "linearly_interpolated_speed_mps": 11,
            },
            {
                "station_id": "a",
                "vehicle_id": "v2",
                "observation_time_seconds": 899,
                "crossing_step_speed_mps": 20,
                "linearly_interpolated_speed_mps": None,
            },
        ],
        interval_start=0,
        interval_end=900,
    )
    assert rows[0]["crossing_count"] == 2
    assert rows[0]["flow_vph"] == 8
    assert rows[0]["crossing_step_speed_mean_mps"] == 15
    with pytest.raises(StationCrossingError, match="incomplete"):
        aggregate_station_interval([], interval_start=0, interval_end=800)


def test_installed_sumo_feasibility_is_subprocess_isolated(tmp_path: Path) -> None:
    output = tmp_path / "native.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analyze_sumo_station_crossing_feasibility",
            "--native-output",
            str(output),
            "--work-directory",
            str(tmp_path / "work"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(output.read_text())
    assert value["arbitrary_within_edge_crossing_observable"] is True
    assert value["restart_equivalent"] is True
    assert value["duplicate_event_count"] == 0
    assert value["direct_departure_crossings"] == {
        "direct-downstream": [],
        "direct-exact": ["station-b"],
        "direct-upstream": ["station-a", "station-b"],
    }
    assert value["multiple_station_through_result"] == ["station-a", "station-b"]
    assert value["arrival_before_station_b_result"] == []
    assert value["checkpoint_boundary_crossing_observed"] is True
    assert value["crossing_method_counts"]["edge_transition"] > 0
    assert value["aggregate_900_seconds"]


def test_artifact_is_deterministic_and_atomic(tmp_path: Path) -> None:
    feasibility = {
        "sumo_version": "1.27.1",
        "fixture_digest": "a" * 64,
        "arbitrary_within_edge_crossing_observable": True,
        "restart_equivalent": True,
        "duplicate_event_count": 0,
        "runtime_overhead_ratio": 1.1,
        "peak_rss_bytes": 100,
    }
    first = build_feasibility_artifact(
        feasibility=feasibility, output_directory=tmp_path / "one"
    )
    second = build_feasibility_artifact(
        feasibility={
            **feasibility,
            "runtime_overhead_ratio": 99.0,
            "peak_rss_bytes": 999,
        },
        output_directory=tmp_path / "two",
    )
    assert first.content_digest == second.content_digest
    assert first.generated_at != ""
    assert not (tmp_path / ".one.pending").exists()
    assert not (tmp_path / ".two.pending").exists()


def _obs(
    edge: str, position: float, *, route_index: int = 0, lane: int = 0, time: float = 1
) -> dict[str, object]:
    return {
        "road_id": edge,
        "lane_id": f"{edge}_{lane}",
        "lane_index": lane,
        "lane_position_m": position,
        "xy": [position, lane],
        "speed_mps": 10.0,
        "route_index": route_index,
        "time_seconds": time,
    }
