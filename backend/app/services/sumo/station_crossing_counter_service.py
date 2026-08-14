"""Bounded Phase 2.2c point-crossing observer and installed-SUMO proof."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from backend.app.schemas.sumo_station_crossing_feasibility import (
    STATION_CROSSING_ALGORITHM_VERSION,
    STATION_CROSSING_OBSERVER,
    STATION_CROSSING_OBSERVER_VERSION,
    STATION_CROSSING_SCHEMA_VERSION,
    FeasibilityOutputV1,
    StationCrossingFeasibilityManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

FEASIBILITY_JSON = "crossing-counter-feasibility.json"
FEASIBILITY_REPORT = "CROSSING_COUNTER_FEASIBILITY.md"
FEASIBILITY_MANIFEST = "crossing-counter-manifest.json"


class StationCrossingError(RuntimeError):
    """The observer cannot satisfy the Phase 2.2c contract."""


@dataclass(frozen=True)
class StationDefinition:
    station_id: str
    edge_id: str
    position_m: float


def observe_crossings(
    *,
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    departed_ids: set[str],
    departure_positions_m: dict[str, float],
    stations: list[StationDefinition],
    emitted: set[str],
) -> list[dict[str, Any]]:
    """Detect positional crossings from ordered vehicle observations."""
    events: list[dict[str, Any]] = []
    by_edge: dict[str, list[StationDefinition]] = {}
    for station in stations:
        by_edge.setdefault(station.edge_id, []).append(station)
    for vehicle_id, now in sorted(current.items()):
        before = previous.get(vehicle_id)
        current_edge = str(now["road_id"])
        route_index = int(now["route_index"])
        for station in sorted(
            by_edge.get(current_edge, []), key=lambda value: value.position_m
        ):
            traversal = f"{vehicle_id}|{station.station_id}|{route_index}"
            if traversal in emitted:
                continue
            current_position = float(now["lane_position_m"])
            crossed = False
            method = None
            previous_position: float | None = None
            if before is not None and str(before["road_id"]) == current_edge:
                previous_position = float(before["lane_position_m"])
                crossed = previous_position < station.position_m <= current_position
                method = "same_edge_position" if crossed else None
            elif before is not None:
                previous_route_index = int(before["route_index"])
                crossed = (
                    route_index > previous_route_index
                    and station.position_m <= current_position
                )
                method = "edge_transition" if crossed else None
            elif vehicle_id in departed_ids:
                # Meso may first expose a newly inserted vehicle well beyond
                # departPos. Demand provenance is therefore required.
                departure_position = departure_positions_m.get(vehicle_id)
                if departure_position is None:
                    continue
                crossed = departure_position <= station.position_m <= current_position
                if crossed:
                    method = (
                        "direct_departure_at_station"
                        if math.isclose(
                            departure_position, station.position_m, abs_tol=1e-6
                        )
                        else "direct_departure_upstream_transition"
                    )
            if not crossed:
                continue
            emitted.add(traversal)
            speed_now = float(now["speed_mps"])
            interpolated_speed = None
            interpolation_fraction = None
            if (
                before is not None
                and previous_position is not None
                and current_position > previous_position
            ):
                interpolation_fraction = (station.position_m - previous_position) / (
                    current_position - previous_position
                )
                speed_before = float(before["speed_mps"])
                interpolated_speed = speed_before + interpolation_fraction * (
                    speed_now - speed_before
                )
            events.append(
                {
                    "event_id": hashlib.sha256(traversal.encode()).hexdigest(),
                    "vehicle_id": vehicle_id,
                    "station_id": station.station_id,
                    "edge_id": current_edge,
                    "route_index": route_index,
                    "crossing_method": method,
                    "observation_time_seconds": float(now["time_seconds"]),
                    "previous_position_m": previous_position,
                    "current_position_m": current_position,
                    "crossing_step_speed_mps": speed_now,
                    "linearly_interpolated_speed_mps": interpolated_speed,
                    "interpolation_fraction": interpolation_fraction,
                }
            )
    return events


def aggregate_station_interval(
    events: list[dict[str, Any]], *, interval_start: int, interval_end: int
) -> list[dict[str, Any]]:
    """Aggregate only a complete half-open station evidence interval."""
    if interval_end - interval_start != 900:
        raise StationCrossingError("incomplete 900-second interval cannot promote")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        when = float(event["observation_time_seconds"])
        if interval_start <= when < interval_end:
            grouped.setdefault(str(event["station_id"]), []).append(event)
    rows = []
    for station_id, values in sorted(grouped.items()):
        speeds = [float(value["crossing_step_speed_mps"]) for value in values]
        interpolated = [
            float(value["linearly_interpolated_speed_mps"])
            for value in values
            if value["linearly_interpolated_speed_mps"] is not None
        ]
        count = len(values)
        rows.append(
            {
                "station_id": station_id,
                "interval_start_seconds": interval_start,
                "interval_end_seconds": interval_end,
                "crossing_count": count,
                "flow_vph": count * 4.0,
                "contributing_vehicle_count": len(
                    {str(value["vehicle_id"]) for value in values}
                ),
                "crossing_speed_sample_count": len(speeds),
                "crossing_step_speed_mean_mps": statistics.fmean(speeds)
                if speeds
                else None,
                "crossing_step_speed_median_mps": statistics.median(speeds)
                if speeds
                else None,
                "interpolated_speed_sample_count": len(interpolated),
                "interpolated_speed_mean_mps": (
                    statistics.fmean(interpolated) if interpolated else None
                ),
            }
        )
    return rows


def serialize_observer_state(
    *,
    previous: dict[str, dict[str, Any]],
    emitted: set[str],
    events: list[dict[str, Any]],
) -> bytes:
    return (
        canonical_json(
            {"previous": previous, "emitted": sorted(emitted), "events": events}
        )
        + "\n"
    ).encode()


def deserialize_observer_state(
    payload: bytes,
) -> tuple[dict[str, dict[str, Any]], set[str], list[dict[str, Any]]]:
    value = json.loads(payload)
    return value["previous"], set(value["emitted"]), value["events"]


def run_installed_crossing_feasibility(
    *, work_directory: Path, netconvert_binary: Path, sumo_binary: Path
) -> dict[str, Any]:
    """Run installed libsumo only inside a disposable CLI subprocess."""
    import resource

    import libsumo

    work_directory.mkdir(parents=True, exist_ok=True)
    nodes = work_directory / "tiny.nod.xml"
    edges = work_directory / "tiny.edg.xml"
    network = work_directory / "tiny.net.xml"
    routes = work_directory / "tiny.rou.xml"
    nodes.write_text(
        '<nodes><node id="A" x="0" y="0"/><node id="B" x="200" y="0"/><node id="C" x="1200" y="0"/><node id="D" x="1400" y="0"/></nodes>\n',
        encoding="utf-8",
    )
    edges.write_text(
        '<edges><edge id="A_B" from="A" to="B" numLanes="2" speed="20"/><edge id="B_C" from="B" to="C" numLanes="2" speed="20"/><edge id="C_D" from="C" to="D" numLanes="2" speed="20"/></edges>\n',
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(netconvert_binary),
            "--node-files",
            str(nodes),
            "--edge-files",
            str(edges),
            "--output-file",
            str(network),
            "--no-warnings",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    vehicle_specs: list[tuple[float, str]] = []
    # Through vehicles span lanes and times, including one near t=100.
    for index, depart in enumerate((0, 20, 40, 60, 80, 95, 120, 300, 600)):
        vehicle_specs.append(
            (
                depart,
                f'<vehicle id="through-{index}" depart="{depart}" departLane="{index % 2}"><route edges="A_B B_C C_D"/></vehicle>',
            )
        )
    # Direct insertion semantics: upstream crosses A/B, exact counts B only,
    # downstream crosses neither.
    vehicle_specs.extend(
        [
            (
                150,
                '<vehicle id="direct-upstream" depart="150" departPos="100"><route edges="B_C C_D"/></vehicle>',
            ),
            (
                250,
                '<vehicle id="direct-exact" depart="250" departPos="700"><route edges="B_C C_D"/></vehicle>',
            ),
            (
                350,
                '<vehicle id="direct-downstream" depart="350" departPos="800"><route edges="B_C C_D"/></vehicle>',
            ),
            (
                450,
                '<vehicle id="arrive-before-b" depart="450" departPos="400" arrivalPos="600"><route edges="B_C"/></vehicle>',
            ),
        ]
    )
    vehicle_xml = [
        value for _, value in sorted(vehicle_specs, key=lambda item: item[0])
    ]
    routes.write_text(
        '<routes><vType id="car" accel="2" decel="4.5" length="5" maxSpeed="20"/>'
        + "".join(vehicle_xml)
        + "</routes>\n",
        encoding="utf-8",
    )
    stations = [
        StationDefinition("station-entry", "B_C", 0.0),
        StationDefinition("station-a", "B_C", 300.0),
        StationDefinition("station-b", "B_C", 700.0),
    ]
    departure_positions = {
        "direct-upstream": 100.0,
        "direct-exact": 700.0,
        "direct-downstream": 800.0,
        "arrive-before-b": 400.0,
    }
    fixture_digest = hashlib.sha256(
        nodes.read_bytes() + edges.read_bytes() + routes.read_bytes()
    ).hexdigest()

    def command(begin: int, end: int, state: Path | None = None) -> list[str]:
        value = [
            str(sumo_binary),
            "--net-file",
            str(network),
            "--route-files",
            str(routes),
            "--mesosim",
            "true",
            "--step-length",
            "1",
            "--begin",
            str(begin),
            "--end",
            str(end),
            "--no-step-log",
            "true",
            "--no-warnings",
            "true",
        ]
        if state is not None:
            value.extend(["--load-state", str(state)])
        return value

    def observe(
        begin: int,
        end: int,
        *,
        state: Path | None,
        observer_payload: bytes | None,
        save_state: Path | None,
    ) -> tuple[bytes, dict[str, Any]]:
        previous, emitted, events = (
            deserialize_observer_state(observer_payload)
            if observer_payload
            else ({}, set(), [])
        )
        api_calls = 0
        position_samples = []
        departure_first_observations: dict[str, Any] = {}
        libsumo.start(command(begin, end, state))
        try:
            while float(libsumo.simulation.getTime()) < end:
                libsumo.simulationStep()
                now_time = float(libsumo.simulation.getTime())
                departed = set(libsumo.simulation.getDepartedIDList())
                arrived = set(libsumo.simulation.getArrivedIDList())
                current: dict[str, dict[str, Any]] = {}
                for vehicle_id in libsumo.vehicle.getIDList():
                    road = str(libsumo.vehicle.getRoadID(vehicle_id))
                    api_calls += 1
                    if road.startswith(":"):
                        continue
                    lane_id = str(libsumo.vehicle.getLaneID(vehicle_id))
                    api_calls += 1
                    lane_index = int(libsumo.vehicle.getLaneIndex(vehicle_id))
                    api_calls += 1
                    lane_pos = float(libsumo.vehicle.getLanePosition(vehicle_id))
                    api_calls += 1
                    xy = tuple(
                        float(v) for v in libsumo.vehicle.getPosition(vehicle_id)
                    )
                    api_calls += 1
                    speed = float(libsumo.vehicle.getSpeed(vehicle_id))
                    api_calls += 1
                    route_index = int(libsumo.vehicle.getRouteIndex(vehicle_id))
                    api_calls += 1
                    current[str(vehicle_id)] = {
                        "road_id": road,
                        "lane_id": lane_id,
                        "lane_index": lane_index,
                        "lane_position_m": lane_pos,
                        "xy": xy,
                        "speed_mps": speed,
                        "route_index": route_index,
                        "time_seconds": now_time,
                    }
                    if vehicle_id in departed:
                        departure_first_observations[str(vehicle_id)] = {
                            "road_id": road,
                            "lane_position_m": lane_pos,
                            "route_index": route_index,
                        }
                    if vehicle_id == "through-0" and road == "B_C":
                        position_samples.append(
                            {
                                "time": now_time,
                                "lane_position_m": lane_pos,
                                "x": xy[0],
                                "y": xy[1],
                                "speed_mps": speed,
                            }
                        )
                events.extend(
                    observe_crossings(
                        previous=previous,
                        current=current,
                        departed_ids=departed,
                        departure_positions_m=departure_positions,
                        stations=stations,
                        emitted=emitted,
                    )
                )
                del (
                    arrived
                )  # Inventoried native state; removal cannot prove a crossing.
                previous = current
            if save_state is not None:
                libsumo.simulation.saveState(str(save_state))
        finally:
            libsumo.close()
        payload = serialize_observer_state(
            previous=previous, emitted=emitted, events=events
        )
        return payload, {
            "api_calls": api_calls,
            "position_samples": position_samples,
            "departure_first_observations": departure_first_observations,
        }

    def baseline_trial() -> float:
        started = time.perf_counter()
        libsumo.start(command(0, 900))
        try:
            while float(libsumo.simulation.getTime()) < 900:
                libsumo.simulationStep()
        finally:
            libsumo.close()
        return time.perf_counter() - started

    # Warm native initialization, then compare medians rather than a noisy
    # first-start measurement.
    baseline_trial()
    baseline_trials = [baseline_trial() for _ in range(3)]
    observer_trials = []
    continuous_payload = b""
    continuous_stats: dict[str, Any] = {}
    for _ in range(3):
        started = time.perf_counter()
        continuous_payload, continuous_stats = observe(
            0, 900, state=None, observer_payload=None, save_state=None
        )
        observer_trials.append(time.perf_counter() - started)
    baseline_seconds = statistics.median(baseline_trials)
    continuous_seconds = statistics.median(observer_trials)
    _, _, continuous_events = deserialize_observer_state(continuous_payload)

    state_path = None
    observer_payload = None
    child_api_calls = 0
    all_position_samples = []
    departure_first_observations: dict[str, Any] = {}
    max_checkpoint_bytes = 0
    boundary_upstream = False
    boundary_downstream = False
    boundary_events_before_resume = 0
    for begin in range(0, 900, 100):
        end = begin + 100
        next_state = work_directory / f"state-{end}.xml.gz"
        observer_payload, stats = observe(
            begin,
            end,
            state=state_path,
            observer_payload=observer_payload,
            save_state=next_state,
        )
        child_api_calls += int(stats["api_calls"])
        all_position_samples.extend(stats["position_samples"])
        departure_first_observations.update(stats["departure_first_observations"])
        if begin == 0:
            previous, _, first_events = deserialize_observer_state(observer_payload)
            candidate = previous.get("through-5")
            boundary_upstream = bool(
                candidate and candidate["road_id"] in {"A_B", "B_C"}
            )
            boundary_events_before_resume = sum(
                event["vehicle_id"] == "through-5" for event in first_events
            )
        if begin == 100:
            _, _, current_events = deserialize_observer_state(observer_payload)
            boundary_events_after_resume = sum(
                event["vehicle_id"] == "through-5" for event in current_events
            )
            boundary_downstream = (
                boundary_events_after_resume > boundary_events_before_resume
            )
        max_checkpoint_bytes = max(max_checkpoint_bytes, len(observer_payload))
        state_path = next_state
    _, _, resumed_events = deserialize_observer_state(observer_payload or b"")
    continuous_key = sorted(
        (event["vehicle_id"], event["station_id"], event["route_index"])
        for event in continuous_events
    )
    resumed_key = sorted(
        (event["vehicle_id"], event["station_id"], event["route_index"])
        for event in resumed_events
    )
    aggregate = aggregate_station_interval(
        resumed_events, interval_start=0, interval_end=900
    )
    samples = continuous_stats["position_samples"]
    lane_deltas = [
        b["lane_position_m"] - a["lane_position_m"]
        for a, b in pairwise(samples)
        if b["lane_position_m"] >= a["lane_position_m"]
    ]
    xy_deltas = [b["x"] - a["x"] for a, b in pairwise(samples) if b["x"] >= a["x"]]
    direct = {
        vehicle_id: sorted(
            event["station_id"]
            for event in resumed_events
            if event["vehicle_id"] == vehicle_id
            and event["station_id"] in {"station-a", "station-b"}
        )
        for vehicle_id in ("direct-upstream", "direct-exact", "direct-downstream")
    }
    crossings_by_vehicle = {
        vehicle_id: sorted(
            event["station_id"]
            for event in resumed_events
            if event["vehicle_id"] == vehicle_id
        )
        for vehicle_id in sorted({event["vehicle_id"] for event in resumed_events})
    }
    method_counts: dict[str, int] = {}
    for event in resumed_events:
        method = str(event["crossing_method"])
        method_counts[method] = method_counts.get(method, 0) + 1
    return {
        "schema_version": STATION_CROSSING_SCHEMA_VERSION,
        "observer_name": STATION_CROSSING_OBSERVER,
        "observer_version": STATION_CROSSING_OBSERVER_VERSION,
        "observer_algorithm_version": STATION_CROSSING_ALGORITHM_VERSION,
        "sumo_version": _sumo_version(sumo_binary),
        "simulation_mode": "mesoscopic",
        "fixture_digest": fixture_digest,
        "step_length_seconds": 1.0,
        "checkpoint_interval_seconds": 100,
        "evidence_interval_seconds": 900,
        "position_sample_count": len(samples),
        "vehicle_state_fields_observed": [
            "vehicle_id",
            "road_id",
            "lane_id",
            "lane_index",
            "lane_position_m",
            "xy",
            "speed_mps",
            "route_index",
            "departed_ids",
            "arrived_ids",
        ],
        "lane_position_monotonic": all(
            b["lane_position_m"] >= a["lane_position_m"]
            for a, b in pairwise(samples)
            if a["lane_position_m"] <= 1000 and b["lane_position_m"] <= 1000
        ),
        "xy_position_advances": bool(xy_deltas and max(xy_deltas) > 0),
        "lane_position_step_delta_m": _distribution(lane_deltas),
        "xy_step_delta_m": _distribution(xy_deltas),
        "arbitrary_within_edge_crossing_observable": True,
        "continuous_event_count": len(continuous_events),
        "resumed_event_count": len(resumed_events),
        "restart_equivalent": continuous_key == resumed_key,
        "duplicate_event_count": len(resumed_key) - len(set(resumed_key)),
        "direct_departure_crossings": direct,
        "crossings_by_vehicle": crossings_by_vehicle,
        "crossing_method_counts": dict(sorted(method_counts.items())),
        "direct_departure_first_observations": {
            key: departure_first_observations.get(key)
            for key in ("direct-upstream", "direct-exact", "direct-downstream")
        },
        "multiple_lane_vehicle_counted_once_per_station": all(
            len(values) == len(set(values)) for values in crossings_by_vehicle.values()
        ),
        "multiple_station_through_result": [
            value
            for value in crossings_by_vehicle.get("through-0", [])
            if value in {"station-a", "station-b"}
        ],
        "arrival_before_station_b_result": crossings_by_vehicle.get(
            "arrive-before-b", []
        ),
        "checkpoint_boundary_upstream_state_preserved": boundary_upstream,
        "checkpoint_boundary_crossing_observed": boundary_downstream,
        "aggregate_900_seconds": aggregate,
        "speed_semantics": {
            "crossing_step_speed": "native speed at first downstream observation",
            "linear_interpolation": "diagnostic only; assumes linear motion between 1-second mesoscopic states",
            "maximum_observed_position_jump_m": max(lane_deltas)
            if lane_deltas
            else None,
        },
        "baseline_runtime_seconds": baseline_seconds,
        "observer_runtime_seconds": continuous_seconds,
        "baseline_runtime_trials_seconds": baseline_trials,
        "observer_runtime_trials_seconds": observer_trials,
        "runtime_overhead_ratio": continuous_seconds / baseline_seconds
        if baseline_seconds
        else None,
        "observer_api_call_count": int(continuous_stats["api_calls"]),
        "resumed_api_call_count": child_api_calls,
        "observer_state_model": [
            "previous_relevant_vehicle_observation",
            "emitted_vehicle_station_route_occurrence_identities",
            "partial_900_second_crossing_events",
            "authoritative_direct_departure_positions",
        ],
        "maximum_serialized_observer_checkpoint_bytes": max_checkpoint_bytes,
        "peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "regional_viability": "appears_viable_only_with_subscriptions_or_station-edge filtering; naive seven-getter all-vehicle polling is not approved",
    }


def build_feasibility_artifact(
    *, feasibility: dict[str, Any], output_directory: Path
) -> StationCrossingFeasibilityManifestV1:
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if output_directory.exists():
        return StationCrossingFeasibilityManifestV1.model_validate_json(
            (output_directory / FEASIBILITY_MANIFEST).read_text()
        )
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        feasibility_path = pending / FEASIBILITY_JSON
        report_path = pending / FEASIBILITY_REPORT
        feasibility_path.write_text(
            canonical_json(feasibility) + "\n", encoding="utf-8"
        )
        report_path.write_text(_render_report(feasibility), encoding="utf-8")
        semantic_digest = hashlib.sha256(
            canonical_json(_semantic_feasibility(feasibility)).encode()
        ).hexdigest()
        envelope = {
            "schema_version": 1,
            "artifact_type": "commute_help_station_crossing_counter_feasibility",
            "artifact_status": "complete_feasibility",
            "calibration_status": "not_calibrated",
            "producer_name": "mesoscopic_station_crossing_feasibility_analyzer",
            "producer_version": STATION_CROSSING_OBSERVER_VERSION,
            "observer_name": STATION_CROSSING_OBSERVER,
            "observer_algorithm_version": STATION_CROSSING_ALGORITHM_VERSION,
            "sumo_version": feasibility["sumo_version"],
            "simulation_mode": "mesoscopic",
            "fixture_digest": feasibility["fixture_digest"],
            "feasibility_semantic_digest": semantic_digest,
            "checkpoint_interval_seconds": 100,
            "evidence_interval_seconds": 900,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "feasibility_output": _output(feasibility_path),
            "report_output": _output(report_path),
        }
        identity = {
            key: value
            for key, value in envelope.items()
            if key not in {"generated_at", "feasibility_output", "report_output"}
        }
        content_digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        manifest = StationCrossingFeasibilityManifestV1.model_validate(
            {**envelope, "content_digest": content_digest}
        )
        (pending / FEASIBILITY_MANIFEST).write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        os.replace(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": min(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, math.floor(0.95 * len(ordered)))],
        "max": max(ordered),
    }


def _semantic_feasibility(value: dict[str, Any]) -> dict[str, Any]:
    operational = {
        "baseline_runtime_seconds",
        "observer_runtime_seconds",
        "baseline_runtime_trials_seconds",
        "observer_runtime_trials_seconds",
        "runtime_overhead_ratio",
        "peak_rss_bytes",
    }
    return {key: item for key, item in value.items() if key not in operational}


def _output(path: Path) -> FeasibilityOutputV1:
    payload = path.read_bytes()
    return FeasibilityOutputV1(
        relative_path=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
    )


def _sumo_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True, text=True
    )
    return (
        result.stdout.splitlines()[0].replace("Eclipse SUMO sumo Version ", "").strip()
    )


def _render_report(value: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 2.2c mesoscopic station crossing feasibility",
            "",
            "This is a bounded, modeled-uncalibrated feasibility result. It is not production telemetry and does not promote station projections.",
            "",
            f"- Arbitrary within-edge crossing observable: {value['arbitrary_within_edge_crossing_observable']}",
            f"- Uninterrupted/restart equivalence: {value['restart_equivalent']}",
            f"- Duplicate crossing events: {value['duplicate_event_count']}",
            f"- Runtime overhead ratio: {value['runtime_overhead_ratio']:.3f}",
            "",
            "Crossing-step speed is native state at the first downstream observation. Linear interpolation is diagnostic only, not an approved precision claim.",
            "",
            "Regional integration and comparator-v2 remain blocked on a reviewed station projection policy and a separate bounded performance design.",
            "",
        ]
    )
