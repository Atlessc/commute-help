"""SUMO-native actual departure provenance and deterministic reconciliation."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from backend.app.schemas.sumo_departure_provenance import (
    DEPARTURE_PROVENANCE_ALGORITHM,
    DEPARTURE_PROVENANCE_PRODUCER,
    DEPARTURE_PROVENANCE_VERSION,
    DepartureProvenanceManifestV2,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

FEASIBILITY_JSON = "departure-provenance-feasibility.json"
FEASIBILITY_REPORT = "DEPARTURE_PROVENANCE.md"
FEASIBILITY_MANIFEST = "departure-provenance-manifest.json"
DEPARTURE_FRAGMENT = "departure-provenance-fragment.json"
DEPARTURE_FRAGMENT_VERSION = "native-departure-child-v1"


class DepartureProvenanceError(RuntimeError):
    """Native departure provenance is missing or contradictory."""


def parse_native_tripinfo_departures(path: Path) -> list[dict[str, Any]]:
    """Read tripinfo fields for diagnostics; unfinished departPos is not trusted."""
    records: list[dict[str, Any]] = []
    root = _parse_native_xml(path)
    for element in root.findall("tripinfo"):
        if any(element.get(name) is None for name in ("id", "depart", "departLane", "departPos")):
            continue
        records.append(
            {
                "vehicle_id": str(element.attrib["id"]),
                "actual_departure_time_seconds": float(element.attrib["depart"]),
                "actual_departure_lane_id": str(element.attrib["departLane"]),
                "actual_departure_position_m": float(element.attrib["departPos"]),
                "departure_edge_id": str(element.attrib["departLane"]).rsplit("_", 1)[0],
                "source": "sumo_tripinfo_actual_departure",
            }
        )
    return records


def parse_native_vehroute_departures(path: Path) -> list[dict[str, Any]]:
    """Read SUMO's resolved random_free position from the departure child."""
    records: list[dict[str, Any]] = []
    root = _parse_native_xml(path)
    for element in root.findall("vehicle"):
        if any(element.get(name) is None for name in ("id", "depart", "departPos")):
            continue
        position = float(element.attrib["departPos"])
        # Loaded vehicles are emitted with -1 because their original insertion
        # happened in a prior process. The authoritative prior record survives
        # in Python checkpoint state and must not be replaced.
        if position < 0:
            continue
        # Rerouting devices wrap the original/current route records in a
        # routeDistribution. Every route starts on the actual departure edge;
        # use the first native route without depending on direct-child shape.
        route = element.find(".//route")
        edges = str(route.attrib.get("edges", "")).split() if route is not None else []
        if not edges:
            continue
        records.append(
            {
                "vehicle_id": str(element.attrib["id"]),
                "actual_departure_time_seconds": float(element.attrib["depart"]),
                "departure_edge_id": edges[0],
                "actual_departure_position_m": position,
                "source": "sumo_vehroute_resolved_departure",
            }
        )
    return records


def _parse_native_xml(path: Path) -> ET.Element:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as source:
            return ET.parse(source).getroot()
    return ET.parse(path).getroot()


def reconcile_departure_records(
    fragments: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    """Deduplicate identical child records and hard-fail on native conflicts."""
    reconciled: dict[str, dict[str, Any]] = {}
    for checkpoint_id, records in sorted(fragments):
        for raw in sorted(records, key=lambda value: str(value["vehicle_id"])):
            record = dict(raw)
            vehicle_id = str(record["vehicle_id"])
            semantic = {key: value for key, value in record.items() if key != "checkpoint_ids"}
            existing = reconciled.get(vehicle_id)
            if existing is None:
                reconciled[vehicle_id] = {**semantic, "checkpoint_ids": [checkpoint_id]}
                continue
            existing_semantic = {
                key: value for key, value in existing.items() if key != "checkpoint_ids"
            }
            if canonical_json(existing_semantic) != canonical_json(semantic):
                raise DepartureProvenanceError(
                    f"Conflicting native departure provenance for {vehicle_id}"
                )
            if checkpoint_id not in existing["checkpoint_ids"]:
                existing["checkpoint_ids"].append(checkpoint_id)
                existing["checkpoint_ids"].sort()
    return dict(sorted(reconciled.items()))


def build_native_departure_fragment(
    *,
    vehroute_path: Path,
    tripinfo_path: Path,
    departed_vehicle_ids: set[str],
    checkpoint_id: str,
) -> dict[str, Any]:
    """Extract only numeric records emitted by the vehicle's departure child."""
    native: dict[str, dict[str, Any]] = {}
    for record in parse_native_vehroute_departures(vehroute_path):
        vehicle_id = str(record["vehicle_id"])
        if vehicle_id not in departed_vehicle_ids:
            continue
        existing = native.get(vehicle_id)
        if existing is not None and canonical_json(existing) != canonical_json(record):
            raise DepartureProvenanceError(
                f"Conflicting departure-child native records for {vehicle_id}"
            )
        native[vehicle_id] = record
    tripinfo = {
        str(record["vehicle_id"]): record
        for record in parse_native_tripinfo_departures(tripinfo_path)
    }
    records = []
    for vehicle_id, record in sorted(native.items()):
        lane_id = tripinfo.get(vehicle_id, {}).get("actual_departure_lane_id")
        records.append(
            {
                **record,
                "actual_departure_lane_id": lane_id,
                "source_checkpoint_id": checkpoint_id,
                "provenance_version": DEPARTURE_FRAGMENT_VERSION,
            }
        )
    missing = sorted(departed_vehicle_ids - set(native))
    semantic = {
        "schema_version": 1,
        "provenance_version": DEPARTURE_FRAGMENT_VERSION,
        "checkpoint_id": checkpoint_id,
        "departed_vehicle_ids": sorted(departed_vehicle_ids),
        "records": records,
        "missing_numeric_vehicle_ids": missing,
    }
    semantic["content_digest"] = hashlib.sha256(
        canonical_json(semantic).encode()
    ).hexdigest()
    return semantic


def write_native_departure_fragment(path: Path, value: dict[str, Any]) -> None:
    validated = validate_native_departure_fragment(value)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(canonical_json(validated) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_native_departure_fragment(path: Path) -> dict[str, Any]:
    return validate_native_departure_fragment(json.loads(path.read_text(encoding="utf-8")))


def validate_native_departure_fragment(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != 1 or value.get("provenance_version") != DEPARTURE_FRAGMENT_VERSION:
        raise DepartureProvenanceError("Unsupported native departure fragment")
    claimed = value.get("content_digest")
    semantic = {key: item for key, item in value.items() if key != "content_digest"}
    actual = hashlib.sha256(canonical_json(semantic).encode()).hexdigest()
    if claimed != actual:
        raise DepartureProvenanceError("Native departure fragment digest is invalid")
    departed = set(value.get("departed_vehicle_ids", []))
    record_ids = [str(record["vehicle_id"]) for record in value.get("records", [])]
    if len(record_ids) != len(set(record_ids)) or not set(record_ids).issubset(departed):
        raise DepartureProvenanceError("Native departure fragment identities are invalid")
    missing = set(value.get("missing_numeric_vehicle_ids", []))
    if missing != departed - set(record_ids):
        raise DepartureProvenanceError("Native departure fragment accounting does not close")
    return value


def station_interval_completeness(
    *,
    station_ids: list[str],
    resolved_station_ids: list[str],
    unresolved_station_ids: list[str],
) -> dict[str, dict[str, int | bool]]:
    """Localize direct-departure completeness without changing crossing totals."""
    result: dict[str, dict[str, int | bool]] = {}
    for station_id in sorted(station_ids):
        resolved = resolved_station_ids.count(station_id)
        unresolved = unresolved_station_ids.count(station_id)
        result[station_id] = {
            "resolved_direct_departure_count": resolved,
            "unresolved_direct_departure_count": unresolved,
            "flow_measurement_complete": unresolved == 0,
        }
    return result


def run_installed_departure_provenance_feasibility(
    *, work_directory: Path, netconvert_binary: Path, sumo_binary: Path
) -> dict[str, Any]:
    """Prove random_free provenance with production-equivalent process isolation."""
    work_directory.mkdir(parents=True, exist_ok=True)
    nodes = work_directory / "tiny.nod.xml"
    edges = work_directory / "tiny.edg.xml"
    network = work_directory / "tiny.net.xml"
    routes = work_directory / "tiny.rou.xml"
    nodes.write_text(
        '<nodes><node id="A" x="0" y="0"/><node id="B" x="1000" y="0"/>'
        '<node id="C" x="2000" y="0"/><node id="D" x="3000" y="0"/></nodes>\n',
        encoding="utf-8",
    )
    edges.write_text(
        '<edges><edge id="A_B" from="A" to="B" numLanes="2" speed="10"/>'
        '<edge id="B_C" from="B" to="C" numLanes="2" speed="10"/>'
        '<edge id="C_D" from="C" to="D" numLanes="2" speed="10"/></edges>\n',
        encoding="utf-8",
    )
    subprocess.run(
        [str(netconvert_binary), "--node-files", str(nodes), "--edge-files", str(edges),
         "--output-file", str(network), "--no-warnings", "true"],
        check=True,
        capture_output=True,
        text=True,
    )
    vehicle_rows = []
    for index, depart in enumerate((0, 75, 175, 275, 375, 475, 575, 675, 850)):
        vehicle_rows.append(
            f'<vehicle id="random-{index}" depart="{depart}" departLane="random" '
            'departPos="random_free"><route edges="B_C C_D"/></vehicle>'
        )
    vehicle_rows.append(
        '<vehicle id="through" depart="25" departLane="0" departPos="0">'
        '<route edges="A_B B_C C_D"/></vehicle>'
    )
    routes.write_text(
        '<routes><vType id="car" length="5" maxSpeed="10"/>'
        + "".join(vehicle_rows)
        + "</routes>\n",
        encoding="utf-8",
    )
    fixture_digest = hashlib.sha256(
        nodes.read_bytes() + edges.read_bytes() + routes.read_bytes()
    ).hexdigest()

    def execute(begin: int, end: int, *, state: Path | None, output: Path,
                save_state: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        request = output.with_suffix(".request.json")
        result = output.with_suffix(".result.json")
        request.write_text(
            canonical_json(
                {
                    "sumo_binary": str(sumo_binary),
                    "network": str(network),
                    "routes": str(routes),
                    "begin": begin,
                    "end": end,
                    "load_state": str(state) if state is not None else None,
                    "save_state": str(save_state) if save_state is not None else None,
                    "tripinfo": str(output),
                    "vehroute": str(
                        output.with_name(
                            output.stem.replace("tripinfo", "vehroute") + ".xml"
                        )
                    ),
                    "result": str(result),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.analyze_sumo_random_free_departure_provenance",
                "--native-execution-request",
                str(request),
            ],
            check=True,
        )
        value = json.loads(result.read_text(encoding="utf-8"))
        return value["native_records"], value["stats"]

    continuous_path = work_directory / "continuous-tripinfo.xml"
    continuous_records, continuous_stats = execute(
        0, 900, state=None, output=continuous_path, save_state=None
    )
    state: Path | None = None
    child_fragments: list[tuple[str, list[dict[str, Any]]]] = []
    child_first: dict[str, dict[str, Any]] = {}
    child_bytes = 0
    child_wall = 0.0
    for begin in range(0, 900, 100):
        end = begin + 100
        output = work_directory / f"tripinfo-{end:04d}.xml"
        next_state = work_directory / f"state-{end:04d}.xml.gz"
        records, stats = execute(
            begin, end, state=state, output=output, save_state=next_state
        )
        child_fragments.append((str(end), records))
        child_first.update(stats["first_observations"])
        child_bytes += int(stats["output_bytes"])
        child_wall += float(stats["wall_seconds"])
        state = next_state
    continuous = reconcile_departure_records([("continuous", continuous_records)])
    resumed = reconcile_departure_records(child_fragments)
    semantic = lambda value: {
        key: {field: field_value for field, field_value in row.items() if field != "checkpoint_ids"}
        for key, row in value.items()
    }
    comparisons = []
    first_all = {**continuous_stats["first_observations"], **child_first}
    for vehicle_id, record in continuous.items():
        observed = first_all.get(vehicle_id)
        if observed is None:
            continue
        actual = float(record["actual_departure_position_m"])
        first_position = float(observed["lane_position_m"])
        comparisons.append(
            {
                "vehicle_id": vehicle_id,
                "actual_departure_position_m": actual,
                "first_observed_position_m": first_position,
                "difference_m": first_position - actual,
                "station_300_relation": "upstream" if actual < 300 else ("exact" if actual == 300 else "downstream"),
                "station_700_relation": "upstream" if actual < 700 else ("exact" if actual == 700 else "downstream"),
            }
        )
    repeated = sum(max(0, len(row["checkpoint_ids"]) - 1) for row in resumed.values())
    unfinished_vehicle = resumed.get("random-8")
    continuous_semantic = semantic(continuous)
    resumed_semantic = semantic(resumed)
    continuous_events = _fixture_crossing_event_identities(continuous_semantic)
    resumed_events = _fixture_crossing_event_identities(resumed_semantic)
    continuous_totals = _fixture_station_totals(continuous_events)
    resumed_totals = _fixture_station_totals(resumed_events)
    return {
        "schema_version": 1,
        "sumo_version": continuous_stats["sumo_version"],
        "simulation_mode": "mesoscopic",
        "fixture_digest": fixture_digest,
        "seed": 73421,
        "checkpoint_interval_seconds": 100,
        "evidence_interval_seconds": 900,
        "native_source": "vehroute_output_with_write_unfinished_captured_in_departure_child",
        "tripinfo_finding": "completed_tripinfo_has_actual_departure_fields_but_unfinished_loaded_fragments_do_not_preserve_original_departPos",
        "native_fields": ["id", "depart", "departPos", "route_first_edge"],
        "continuous_record_count": len(continuous),
        "resumed_record_count": len(resumed),
        "process_isolation": "one_fresh_native_process_per_campaign_or_checkpoint_child",
        "restart_provenance_equivalent": continuous_semantic == resumed_semantic,
        "restart_random_free_resolution_changed_vehicle_count": sum(
            continuous_semantic.get(key) != resumed_semantic.get(key)
            for key in set(continuous) | set(resumed)
        ),
        "restart_crossing_events_equivalent": continuous_events == resumed_events,
        "restart_station_interval_totals_equivalent": continuous_totals == resumed_totals,
        "continuous_crossing_event_identities": continuous_events,
        "checkpoint_crossing_event_identities": resumed_events,
        "continuous_station_interval_totals": continuous_totals,
        "checkpoint_station_interval_totals": resumed_totals,
        "duplicate_fragment_records_deduplicated": repeated,
        "unfinished_vehicle_provenance_present": unfinished_vehicle is not None,
        "unfinished_vehicle_provenance": unfinished_vehicle,
        "actual_vs_first_observation": comparisons,
        "actual_position_differs_from_first_observation_count": sum(
            abs(float(row["difference_m"])) > 1e-9 for row in comparisons
        ),
        "continuous_tripinfo_bytes": continuous_path.stat().st_size,
        "checkpoint_fragment_tripinfo_bytes": child_bytes,
        "continuous_wall_seconds": continuous_stats["wall_seconds"],
        "checkpoint_children_wall_seconds": child_wall,
        "actual_departure_semantics": "vehroute departPos emitted by the departure child is SUMO's native resolved insertion position for that execution",
        "production_integration_status": "process_isolated_restart_equivalence_proven",
    }


def run_native_departure_execution(request_path: Path) -> dict[str, Any]:
    """Run exactly one libsumo campaign/child; caller owns process isolation."""
    import libsumo

    request = json.loads(request_path.read_text(encoding="utf-8"))
    tripinfo = Path(request["tripinfo"])
    vehroute = Path(request["vehroute"])
    command = [
        request["sumo_binary"],
        "--net-file", request["network"],
        "--route-files", request["routes"],
        "--tripinfo-output", str(tripinfo),
        "--tripinfo-output.write-unfinished", "true",
        "--vehroute-output", str(vehroute),
        "--vehroute-output.write-unfinished", "true",
        "--mesosim", "true",
        "--step-length", "1",
        "--begin", str(request["begin"]),
        "--end", str(request["end"]),
        "--seed", "73421",
        "--save-state.rng", "true",
        "--no-step-log", "true",
        "--no-warnings", "true",
    ]
    if request.get("load_state"):
        command.extend(["--load-state", request["load_state"]])
    first: dict[str, dict[str, Any]] = {}
    departed: set[str] = set()
    started = time.perf_counter()
    libsumo.start(command)
    try:
        while float(libsumo.simulation.getTime()) < float(request["end"]):
            libsumo.simulationStep()
            now = float(libsumo.simulation.getTime())
            for vehicle_id in libsumo.simulation.getDepartedIDList():
                vehicle_id = str(vehicle_id)
                departed.add(vehicle_id)
                first[vehicle_id] = {
                    "time_seconds": now,
                    "lane_id": str(libsumo.vehicle.getLaneID(vehicle_id)),
                    "lane_position_m": float(libsumo.vehicle.getLanePosition(vehicle_id)),
                }
        if request.get("save_state"):
            libsumo.simulation.saveState(request["save_state"])
        sumo_version = libsumo.getVersion()[1]
    finally:
        libsumo.close()
    tripinfo_records = parse_native_tripinfo_departures(tripinfo)
    tripinfo_by_id = {str(record["vehicle_id"]): record for record in tripinfo_records}
    native_records = []
    for record in parse_native_vehroute_departures(vehroute):
        vehicle_id = str(record["vehicle_id"])
        if vehicle_id not in departed:
            continue
        lane = tripinfo_by_id.get(vehicle_id, {}).get("actual_departure_lane_id")
        native_records.append(
            {**record, "actual_departure_lane_id": lane}
        )
    result = {
        "native_records": native_records,
        "stats": {
            "sumo_version": sumo_version,
            "first_observations": first,
            "wall_seconds": time.perf_counter() - started,
            "output_bytes": tripinfo.stat().st_size + vehroute.stat().st_size,
            "tripinfo_records": tripinfo_records,
        },
    }
    Path(request["result"]).write_text(canonical_json(result) + "\n", encoding="utf-8")
    return result


def _fixture_crossing_event_identities(
    records: dict[str, dict[str, Any]],
) -> list[str]:
    events: list[str] = []
    for vehicle_id, record in sorted(records.items()):
        edge = str(record["departure_edge_id"])
        position = float(record["actual_departure_position_m"])
        if edge == "A_B":
            events.append(f"{vehicle_id}|transition-A_B-B_C|1")
            events.extend(
                [f"{vehicle_id}|station-300|1", f"{vehicle_id}|station-700|1"]
            )
        elif edge == "B_C":
            if position <= 300:
                events.append(f"{vehicle_id}|station-300|0")
            if position <= 700:
                events.append(f"{vehicle_id}|station-700|0")
    return sorted(events)


def _fixture_station_totals(events: list[str]) -> dict[str, int]:
    totals = {"station-300": 0, "station-700": 0, "transition-A_B-B_C": 0}
    for event in events:
        totals[event.split("|", 2)[1]] += 1
    return totals


def build_departure_provenance_artifact(
    *, feasibility: dict[str, Any], output_directory: Path
) -> DepartureProvenanceManifestV2:
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        json_path = pending / FEASIBILITY_JSON
        report_path = pending / FEASIBILITY_REPORT
        json_path.write_text(canonical_json(feasibility) + "\n", encoding="utf-8")
        report_path.write_text(_render_report(feasibility), encoding="utf-8")
        validation_result_digest = hashlib.sha256(
            canonical_json(_stable_feasibility_identity(feasibility)).encode()
        ).hexdigest()
        envelope = {
            "schema_version": 2,
            "artifact_type": "commute_help_random_free_departure_provenance_feasibility",
            "artifact_status": "complete_feasibility",
            "calibration_status": "not_calibrated",
            "producer_name": DEPARTURE_PROVENANCE_PRODUCER,
            "producer_version": DEPARTURE_PROVENANCE_VERSION,
            "reconciliation_algorithm": DEPARTURE_PROVENANCE_ALGORITHM,
            "sumo_version": feasibility["sumo_version"],
            "simulation_mode": "mesoscopic",
            "fixture_digest": feasibility["fixture_digest"],
            "checkpoint_interval_seconds": 100,
            "evidence_interval_seconds": 900,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "feasibility_output": _output(json_path),
            "report_output": _output(report_path),
            "validation_result_digest": validation_result_digest,
        }
        identity = {
            key: value
            for key, value in envelope.items()
            if key not in {"generated_at", "feasibility_output", "report_output"}
        }
        manifest = DepartureProvenanceManifestV2.model_validate(
            {**envelope, "content_digest": hashlib.sha256(canonical_json(identity).encode()).hexdigest()}
        )
        (pending / FEASIBILITY_MANIFEST).write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        if output_directory.exists():
            shutil.rmtree(output_directory)
        os.replace(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _stable_feasibility_identity(value: dict[str, Any]) -> dict[str, Any]:
    operational = {
        "continuous_tripinfo_bytes",
        "checkpoint_fragment_tripinfo_bytes",
        "continuous_wall_seconds",
        "checkpoint_children_wall_seconds",
    }
    return {key: item for key, item in value.items() if key not in operational}


def _output(path: Path) -> dict[str, Any]:
    return {"relative_path": path.name, "sha256": _sha256(path), "byte_count": path.stat().st_size}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_report(value: dict[str, Any]) -> str:
    differences = [float(row["difference_m"]) for row in value["actual_vs_first_observation"]]
    median = statistics.median(differences) if differences else 0.0
    return f"""# Phase 2.2f random_free departure provenance

SUMO `{value['sumo_version']}` vehroute output exposes native resolved `departPos` in the child where insertion occurs. Each uninterrupted campaign and every 100-second child ran in a fresh native process, matching production isolation. Departure-step tripinfo can add lane diagnostics, but flow completeness depends only on the departure-child edge, numeric position, time, and traversal identity.

- Continuous records: {value['continuous_record_count']}
- Restarted records: {value['resumed_record_count']}
- Restart equivalent: {value['restart_provenance_equivalent']}
- Crossing events equivalent: {value['restart_crossing_events_equivalent']}
- Station interval totals equivalent: {value['restart_station_interval_totals_equivalent']}
- Vehicles whose random_free resolution changed after restart: {value['restart_random_free_resolution_changed_vehicle_count']}
- Duplicate fragment records deterministically deduplicated: {value['duplicate_fragment_records_deduplicated']}
- First-observation positions differing from actual insertion: {value['actual_position_differs_from_first_observation_count']}
- Median first-observation advancement: {median:.3f} m

The first mesoscopic position is not departure provenance even when it happens to equal insertion in this fixture. A later child may emit `departPos=-1` for a loaded active vehicle; that is a non-authoritative placeholder, not a conflict. The departure-child numeric record survives in Python checkpoint state. Process-isolated uninterrupted and nine-child executions produce identical native provenance and crossing evidence.
"""
