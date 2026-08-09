"""Run one deterministic local SUMO request from a bounded JSON manifest."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import quoteattr

import libsumo

from backend.app.services.sumo.output_service import parse_tripinfo


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    run_dir = request_path.parent.resolve()
    fixture_dir = Path(request["fixture_dir"]).resolve()
    if not _inside(fixture_dir, Path.cwd()) or not _inside(run_dir, Path.cwd()):
        raise ValueError("Worker inputs must remain inside the local repository")
    result_path = run_dir / "result.json"
    progress_path = run_dir / "progress.json"
    cancel_path = run_dir / "cancel.requested"
    network_path = run_dir / "tiny.net.xml"
    routes_path = run_dir / "tiny.rou.xml"
    tripinfo_path = run_dir / "tripinfo.xml"
    netconvert = str(request["netconvert_binary"])
    subprocess.run(
        [
            netconvert,
            "--node-files",
            str(fixture_dir / "tiny.nod.xml"),
            "--edge-files",
            str(fixture_dir / "tiny.edg.xml"),
            "--output-file",
            str(network_path),
            "--no-warnings",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    vehicle_id = "selected-trip"
    routes_path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                "<routes>",
                '  <vType id="passenger" accel="2.6" decel="4.5" length="5" maxSpeed="13.89"/>',
                f"  <vehicle id={quoteattr(vehicle_id)} type=\"passenger\" depart=\"0\">",
                '    <route edges="A_B B_C C_F"/>',
                "  </vehicle>",
                "</routes>",
                "",
            ]
        ),
        encoding="utf-8",
    )
    seed = int(request["seed"])
    step_delay = max(0.0, float(request.get("step_delay_ms", 0)) / 1000.0)
    initial_route: list[str] = []
    final_route: list[str] = []
    closure_applied = False
    cancelled = False
    libsumo.start(
        [
            str(request["sumo_binary"]),
            "--net-file",
            str(network_path),
            "--route-files",
            str(routes_path),
            "--tripinfo-output",
            str(tripinfo_path),
            "--seed",
            str(seed),
            "--no-step-log",
            "true",
            "--no-warnings",
            "true",
        ]
    )
    try:
        while libsumo.simulation.getMinExpectedNumber() > 0:
            if cancel_path.exists():
                cancelled = True
                break
            libsumo.simulationStep()
            sim_time = float(libsumo.simulation.getTime())
            active = set(libsumo.vehicle.getIDList())
            if vehicle_id in active and not initial_route:
                initial_route = list(libsumo.vehicle.getRoute(vehicle_id))
            if vehicle_id in active and sim_time >= 1 and not closure_applied:
                libsumo.lane.setDisallowed("B_C_0", ["passenger"])
                libsumo.vehicle.rerouteTraveltime(vehicle_id)
                final_route = list(libsumo.vehicle.getRoute(vehicle_id))
                closure_applied = True
            _atomic_json(
                progress_path,
                {"status": "running", "sim_second": sim_time, "progress": min(sim_time / 60, 0.99)},
            )
            if step_delay:
                time.sleep(step_delay)
    finally:
        libsumo.close()

    if cancelled:
        _atomic_json(result_path, {"status": "cancelled", "seed": seed})
        return 0
    trip = parse_tripinfo(tripinfo_path, vehicle_id)
    result = {
        "status": "completed",
        "seed": seed,
        "closure_edge": "B_C",
        "closure_applied": closure_applied,
        "initial_route": initial_route,
        "final_route": final_route,
        "rerouted": "B_C" in initial_route and "B_C" not in final_route,
        "trip": trip,
    }
    _atomic_json(result_path, result)
    _atomic_json(progress_path, {"status": "completed", "sim_second": trip["arrival"], "progress": 1.0})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    return run(args.request.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
