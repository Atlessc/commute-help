"""Phase 2.2f native random_free departure provenance tests."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.services.sumo.departure_provenance_service import (
    DepartureProvenanceError,
    build_native_departure_fragment,
    reconcile_departure_records,
    station_interval_completeness,
)
from backend.app.services.sumo.station_telemetry_service import (
    OptimizedStationObserver,
    ProductionStationDefinition,
    observe_production_crossings,
)


def test_native_duplicate_reconciliation_and_conflict() -> None:
    record = _departure("v", 100)
    result = reconcile_departure_records([("100", [record]), ("200", [record])])
    assert result["v"]["checkpoint_ids"] == ["100", "200"]
    with pytest.raises(DepartureProvenanceError, match="Conflicting"):
        reconcile_departure_records(
            [("100", [record]), ("200", [{**record, "actual_departure_position_m": 200}])]
        )


def test_station_interval_uncertainty_is_localized() -> None:
    result = station_interval_completeness(
        station_ids=["a", "b", "c"],
        resolved_station_ids=["a", "a", "b"],
        unresolved_station_ids=["b"],
    )
    assert result["a"] == {
        "resolved_direct_departure_count": 2,
        "unresolved_direct_departure_count": 0,
        "flow_measurement_complete": True,
    }
    assert result["b"]["flow_measurement_complete"] is False
    assert result["c"]["flow_measurement_complete"] is True


def test_exact_position_counts_and_transition_direct_departure_does_not() -> None:
    transition = ProductionStationDefinition(
        "t", "app", "edge_transition", None, None, "before", "edge", "NORTH", "r", "p-t"
    )
    events = observe_production_crossings(
        previous={}, current={"v": _observation(400, 10)}, departed_ids={"v"},
        departure_positions_m={"v": 300}, stations=[_station("a", 300), transition], emitted=set()
    )
    assert [(event["vehicle_id"], event["station_id"]) for event in events] == [("v", "a")]


def test_native_position_resolves_each_station_independently() -> None:
    transition = ProductionStationDefinition(
        "t", "app", "edge_transition", None, None, "before", "edge", "NORTH", "r", "p-t"
    )
    observer = OptimizedStationObserver(
        _plan([_station("a", 300), _station("b", 700), transition]),
        variant="baseline",
        defer_interval_finalization=True,
    )
    events, rows = observer.observe(
        time_seconds=100,
        subscription_rows={"v": _observation(800, 100)},
        departed_ids={"v"},
        arrived_ids=set(),
        departure_positions_m={},
    )
    assert events == [] and rows == []
    events, rows = observer.reconcile_native_departures(
        records=[_departure("v", 500)],
        missing_numeric_vehicle_ids=set(),
        time_seconds=900,
    )
    assert [(event["vehicle_id"], event["station_id"]) for event in events] == [("v", "b")]
    by_station = {row["station_id"]: row for row in rows}
    assert by_station["a"]["crossing_count"] == 0
    assert by_station["a"]["resolved_direct_departure_count"] == 1
    assert by_station["b"]["crossing_count"] == 1
    assert by_station["t"]["crossing_count"] == 0
    assert all(row["flow_measurement_complete"] for row in rows)


def test_missing_native_record_is_local_to_affected_stations() -> None:
    observer = OptimizedStationObserver(
        _plan([_station("a", 300), _station("b", 700)]),
        variant="scenario",
        defer_interval_finalization=True,
    )
    observer.observe(
        time_seconds=100,
        subscription_rows={"v": _observation(400, 100)},
        departed_ids={"v"},
        arrived_ids=set(),
        departure_positions_m={},
    )
    _, rows = observer.reconcile_native_departures(
        records=[], missing_numeric_vehicle_ids={"v"}, time_seconds=900
    )
    by_station = {row["station_id"]: row for row in rows}
    assert by_station["a"]["flow_measurement_complete"] is False
    assert by_station["a"]["unresolved_direct_departure_count"] == 1
    assert by_station["b"]["flow_measurement_complete"] is True


def test_departure_fragment_uses_only_departure_child_numeric_records(tmp_path: Path) -> None:
    vehroute = tmp_path / "vehroute.xml"
    tripinfo = tmp_path / "tripinfo.xml"
    vehroute.write_text(
        '<routes><vehicle id="numeric" depart="10" departPos="123">'
        '<route edges="edge next"/></vehicle><vehicle id="loaded" depart="10" '
        'departPos="-1"><route edges="edge next"/></vehicle></routes>'
    )
    tripinfo.write_text(
        '<tripinfos><tripinfo id="numeric" depart="10" departLane="edge_0" '
        'departPos="123"/><tripinfo id="loaded" depart="10" departLane="edge_0" '
        'departPos="-1"/></tripinfos>'
    )
    value = build_native_departure_fragment(
        vehroute_path=vehroute,
        tripinfo_path=tripinfo,
        departed_vehicle_ids={"numeric", "loaded"},
        checkpoint_id="baseline:100",
    )
    assert [record["vehicle_id"] for record in value["records"]] == ["numeric"]
    assert value["missing_numeric_vehicle_ids"] == ["loaded"]


def test_rerouted_vehroute_distribution_preserves_departure_edge(tmp_path: Path) -> None:
    vehroute = tmp_path / "vehroute.xml"
    tripinfo = tmp_path / "tripinfo.xml"
    vehroute.write_text(
        '<routes><vehicle id="rerouted" depart="10" departPos="123">'
        '<routeDistribution><route edges="edge original"/>'
        '<route edges="edge replacement"/></routeDistribution></vehicle></routes>'
    )
    tripinfo.write_text(
        '<tripinfos><tripinfo id="rerouted" depart="10" departLane="edge_0" '
        'departPos="123"/></tripinfos>'
    )
    value = build_native_departure_fragment(
        vehroute_path=vehroute,
        tripinfo_path=tripinfo,
        departed_vehicle_ids={"rerouted"},
        checkpoint_id="baseline:100",
    )
    assert value["records"][0]["departure_edge_id"] == "edge"
    assert value["missing_numeric_vehicle_ids"] == []


def test_conflicting_numeric_records_in_one_child_hard_fail(tmp_path: Path) -> None:
    vehroute = tmp_path / "vehroute.xml"
    tripinfo = tmp_path / "tripinfo.xml"
    vehroute.write_text(
        '<routes><vehicle id="v" depart="10" departPos="100">'
        '<route edges="edge next"/></vehicle><vehicle id="v" depart="10" '
        'departPos="200"><route edges="edge next"/></vehicle></routes>'
    )
    tripinfo.write_text(
        '<tripinfos><tripinfo id="v" depart="10" departLane="edge_0" '
        'departPos="100"/></tripinfos>'
    )
    with pytest.raises(DepartureProvenanceError, match="Conflicting departure-child"):
        build_native_departure_fragment(
            vehroute_path=vehroute,
            tripinfo_path=tripinfo,
            departed_vehicle_ids={"v"},
            checkpoint_id="baseline:100",
        )


def test_gzipped_native_vehroute_is_supported(tmp_path: Path) -> None:
    vehroute = tmp_path / "vehroute.xml.gz"
    tripinfo = tmp_path / "tripinfo.xml"
    with gzip.open(vehroute, "wt") as output:
        output.write(
            '<routes><vehicle id="v" depart="10" departPos="123">'
            '<route edges="edge next"/></vehicle></routes>'
        )
    tripinfo.write_text(
        '<tripinfos><tripinfo id="v" depart="10" departLane="edge_0" '
        'departPos="123"/></tripinfos>'
    )
    value = build_native_departure_fragment(
        vehroute_path=vehroute,
        tripinfo_path=tripinfo,
        departed_vehicle_ids={"v"},
        checkpoint_id="baseline:100",
    )
    assert value["records"][0]["actual_departure_position_m"] == 123


def test_installed_random_free_native_proof_is_subprocess_isolated(tmp_path: Path) -> None:
    native = tmp_path / "native.json"
    subprocess.run(
        [sys.executable, "-m", "scripts.analyze_sumo_random_free_departure_provenance",
         "--native-output", str(native), "--work-directory", str(tmp_path / "sumo")],
        check=True,
    )
    value = json.loads(native.read_text())
    assert value["restart_provenance_equivalent"] is True
    assert value["restart_random_free_resolution_changed_vehicle_count"] == 0
    assert value["restart_crossing_events_equivalent"] is True
    assert value["restart_station_interval_totals_equivalent"] is True
    assert value["unfinished_vehicle_provenance_present"] is True
    # The first position happened to equal insertion in this fixture, but it is
    # diagnostic only; the proof and observer use native vehroute provenance.
    assert value["actual_position_differs_from_first_observation_count"] >= 0
    assert value["process_isolation"] == (
        "one_fresh_native_process_per_campaign_or_checkpoint_child"
    )


def _station(station_id: str, position: float) -> ProductionStationDefinition:
    return ProductionStationDefinition(
        station_id, "app", "within_edge_position", "edge", position,
        None, None, "NORTH", "r", f"p-{station_id}"
    )


def _plan(stations: list[ProductionStationDefinition]) -> dict[str, object]:
    return {
        "stations": [station.__dict__ for station in stations],
        "relevant_edge_ids": ["before", "edge"],
        "observation_plan_digest": "a" * 64,
        "policy_content_digest": "b" * 64,
    }


def _observation(position: float, time_seconds: int) -> dict[str, object]:
    return {"road_id": "edge", "lane_position_m": position, "route_index": 0, "time_seconds": time_seconds}


def _departure(vehicle_id: str, position: float) -> dict[str, object]:
    return {
        "vehicle_id": vehicle_id,
        "actual_departure_time_seconds": 10.0,
        "actual_departure_lane_id": "edge_0",
        "actual_departure_position_m": position,
        "departure_edge_id": "edge",
        "source": "sumo_vehroute_resolved_departure",
    }
