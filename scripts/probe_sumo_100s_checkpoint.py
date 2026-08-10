#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = ROOT / "data" / "sumo" / "chunk-probe-100s"


def sha256_strings(values: list[str]) -> str:
    digest = hashlib.sha256()

    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")

    return digest.hexdigest()


def newest_baseline_request() -> Path:
    candidates = list(
        (ROOT / "data" / "sumo" / "runs").glob(
            "*/baseline-variant-request.json"
        )
    )

    if not candidates:
        raise RuntimeError(
            "No baseline-variant-request.json files found under "
            "data/sumo/runs"
        )

    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_command(
    request: dict[str, Any],
    *,
    begin: int,
    end: int,
    load_state: Path | None = None,
) -> list[str]:
    payload = request["payload"]

    command = [
        str(request["sumo_binary"]),
        "--net-file",
        str(request["network_path"]),
        "--route-files",
        str(request["route_path"]),
        "--begin",
        str(begin),
        "--end",
        str(end),
        "--seed",
        str(payload["seed"]),
        "--mesosim",
        "true",
        "--routing-algorithm",
        "astar",
        "--device.rerouting.probability",
        "1",
        "--device.rerouting.mode",
        "8",
        "--device.rerouting.period",
        str(payload["reroute_period_seconds"]),
        "--device.rerouting.adaptation-steps",
        "6",
        "--device.rerouting.adaptation-interval",
        "10",
        "--device.rerouting.threads",
        "6",
        "--time-to-teleport",
        "300",
        "--no-step-log",
        "true",
        "--no-warnings",
        "true",

        # Critical for deterministic checkpoint continuation.
        "--save-state.rng",
        "true",
    ]

    if load_state is not None:
        command.extend(
            [
                "--load-state",
                str(load_state),
            ]
        )

    return command


def snapshot(libsumo: Any) -> dict[str, Any]:
    active_ids = sorted(
        str(vehicle_id)
        for vehicle_id in libsumo.vehicle.getIDList()
    )

    road_records: list[str] = []

    for vehicle_id in active_ids:
        try:
            road = str(libsumo.vehicle.getRoadID(vehicle_id))
        except Exception:
            road = "<error>"

        road_records.append(f"{vehicle_id}\t{road}")

    return {
        "sim_time": float(libsumo.simulation.getTime()),
        "active_vehicle_count": len(active_ids),
        "active_vehicle_ids_sha256": sha256_strings(active_ids),
        "active_vehicle_roads_sha256": sha256_strings(
            road_records
        ),
        "min_expected_number": int(
            libsumo.simulation.getMinExpectedNumber()
        ),
    }


def advance(
    libsumo: Any,
    *,
    target_second: int,
) -> dict[str, int]:
    departed = 0
    arrived = 0
    teleports = 0

    while float(libsumo.simulation.getTime()) < target_second:
        libsumo.simulationStep()

        departed += int(
            libsumo.simulation.getDepartedNumber()
        )

        arrived += int(
            libsumo.simulation.getArrivedNumber()
        )

        teleports += len(
            libsumo.simulation.getStartingTeleportIDList()
        )

    return {
        "departed": departed,
        "arrived": arrived,
        "teleports": teleports,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_child(
    mode: str,
    request_path: Path,
    output_path: Path,
    state_path: Path,
) -> int:
    request = json.loads(
        request_path.read_text(encoding="utf-8")
    )

    import libsumo

    started = time.perf_counter()

    if mode == "continuous":
        command = build_command(
            request,
            begin=0,
            end=200,
        )

        libsumo.start(command)

        try:
            first = advance(
                libsumo,
                target_second=100,
            )

            snapshot_100 = snapshot(libsumo)

            second = advance(
                libsumo,
                target_second=200,
            )

            snapshot_200 = snapshot(libsumo)

        finally:
            libsumo.close()

        result = {
            "mode": mode,
            "first_half": first,
            "second_half": second,
            "totals": {
                key: first[key] + second[key]
                for key in first
            },
            "snapshot_100": snapshot_100,
            "snapshot_200": snapshot_200,
            "wall_seconds": round(
                time.perf_counter() - started,
                3,
            ),
        }

    elif mode == "chunk1":
        command = build_command(
            request,
            begin=0,
            end=200,
        )

        libsumo.start(command)

        try:
            counts = advance(
                libsumo,
                target_second=100,
            )

            snap = snapshot(libsumo)

            libsumo.simulation.saveState(
                str(state_path)
            )

        finally:
            libsumo.close()

        if not state_path.is_file():
            raise RuntimeError(
                f"SUMO did not create checkpoint: {state_path}"
            )

        result = {
            "mode": mode,
            "counts": counts,
            "snapshot_100": snap,
            "checkpoint": str(state_path),
            "checkpoint_bytes": state_path.stat().st_size,
            "wall_seconds": round(
                time.perf_counter() - started,
                3,
            ),
        }

    elif mode == "chunk2":
        if not state_path.is_file():
            raise RuntimeError(
                f"Missing checkpoint: {state_path}"
            )

        command = build_command(
            request,
            begin=100,
            end=200,
            load_state=state_path,
        )

        libsumo.start(command)

        try:
            loaded_snapshot = snapshot(libsumo)

            counts = advance(
                libsumo,
                target_second=200,
            )

            final_snapshot = snapshot(libsumo)

        finally:
            libsumo.close()

        result = {
            "mode": mode,
            "loaded_snapshot": loaded_snapshot,
            "counts": counts,
            "snapshot_200": final_snapshot,
            "wall_seconds": round(
                time.perf_counter() - started,
                3,
            ),
        }

    else:
        raise ValueError(f"Unknown child mode: {mode}")

    write_json(output_path, result)

    return 0


def run_process(
    mode: str,
    request_path: Path,
    result_path: Path,
    state_path: Path,
) -> None:
    print()
    print(f"▶ {mode}")

    started = time.perf_counter()

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            mode,
            "--request",
            str(request_path),
            "--output",
            str(result_path),
            "--state",
            str(state_path),
        ],
        cwd=ROOT,
        check=True,
    )

    elapsed = time.perf_counter() - started

    print(f"  completed in {elapsed:.1f}s")


def compare() -> int:
    request_path = newest_baseline_request()

    print("SUMO 100-second checkpoint equivalence probe")
    print("============================================")
    print()
    print("Source request:")
    print(request_path.relative_to(ROOT))

    request = json.loads(
        request_path.read_text(encoding="utf-8")
    )

    print()
    print("Network:")
    print(request["network_path"])

    print()
    print("Demand:")
    print(request["route_path"])

    print()
    print("Seed:")
    print(request["payload"]["seed"])

    print()
    print("Reroute period:")
    print(
        request["payload"]["reroute_period_seconds"],
        "seconds",
    )

    if PROBE_DIR.exists():
        shutil.rmtree(PROBE_DIR)

    PROBE_DIR.mkdir(parents=True)

    copied_request = PROBE_DIR / "source-request.json"
    shutil.copy2(request_path, copied_request)

    state_path = PROBE_DIR / "checkpoint-00000100.xml.gz"

    continuous_path = PROBE_DIR / "continuous.json"
    chunk1_path = PROBE_DIR / "chunk1.json"
    chunk2_path = PROBE_DIR / "chunk2.json"

    # Control run.
    run_process(
        "continuous",
        copied_request,
        continuous_path,
        state_path,
    )

    # 0 -> 100, save and completely exit libsumo process.
    run_process(
        "chunk1",
        copied_request,
        chunk1_path,
        state_path,
    )

    # Brand-new Python process loads state and goes 100 -> 200.
    run_process(
        "chunk2",
        copied_request,
        chunk2_path,
        state_path,
    )

    continuous = json.loads(
        continuous_path.read_text(encoding="utf-8")
    )

    chunk1 = json.loads(
        chunk1_path.read_text(encoding="utf-8")
    )

    chunk2 = json.loads(
        chunk2_path.read_text(encoding="utf-8")
    )

    chunk_totals = {
        key: (
            int(chunk1["counts"][key])
            + int(chunk2["counts"][key])
        )
        for key in (
            "departed",
            "arrived",
            "teleports",
        )
    }

    comparisons = {
        "checkpoint time exactly 100": (
            chunk2["loaded_snapshot"]["sim_time"]
            == 100.0
        ),
        "final time exactly 200": (
            chunk2["snapshot_200"]["sim_time"]
            == 200.0
        ),
        "departed totals": (
            chunk_totals["departed"]
            == continuous["totals"]["departed"]
        ),
        "arrived totals": (
            chunk_totals["arrived"]
            == continuous["totals"]["arrived"]
        ),
        "teleport totals": (
            chunk_totals["teleports"]
            == continuous["totals"]["teleports"]
        ),
        "active vehicle count": (
            chunk2["snapshot_200"]["active_vehicle_count"]
            == continuous["snapshot_200"]["active_vehicle_count"]
        ),
        "active vehicle IDs": (
            chunk2["snapshot_200"][
                "active_vehicle_ids_sha256"
            ]
            == continuous["snapshot_200"][
                "active_vehicle_ids_sha256"
            ]
        ),
        "active vehicle roads": (
            chunk2["snapshot_200"][
                "active_vehicle_roads_sha256"
            ]
            == continuous["snapshot_200"][
                "active_vehicle_roads_sha256"
            ]
        ),
        "minimum expected vehicles": (
            chunk2["snapshot_200"]["min_expected_number"]
            == continuous["snapshot_200"]["min_expected_number"]
        ),
    }

    print()
    print("RESULTS")
    print("=======")

    print()
    print(
        f"Continuous wall time : "
        f"{continuous['wall_seconds']:.1f}s"
    )

    chunked_wall = (
        float(chunk1["wall_seconds"])
        + float(chunk2["wall_seconds"])
    )

    print(
        f"Chunked wall time    : "
        f"{chunked_wall:.1f}s"
    )

    overhead = chunked_wall - float(
        continuous["wall_seconds"]
    )

    print(
        f"Chunk overhead       : "
        f"{overhead:+.1f}s"
    )

    print()
    print("Continuous totals:")
    print(json.dumps(
        continuous["totals"],
        indent=2,
    ))

    print()
    print("Chunked totals:")
    print(json.dumps(
        chunk_totals,
        indent=2,
    ))

    print()
    print("Equivalence:")

    passed = True

    for label, value in comparisons.items():
        marker = "PASS" if value else "FAIL"
        print(f"  {marker:4}  {label}")

        if not value:
            passed = False

    summary = {
        "passed": passed,
        "continuous_wall_seconds": continuous[
            "wall_seconds"
        ],
        "chunked_wall_seconds": round(
            chunked_wall,
            3,
        ),
        "chunk_overhead_seconds": round(
            overhead,
            3,
        ),
        "comparisons": comparisons,
    }

    write_json(
        PROBE_DIR / "summary.json",
        summary,
    )

    print()
    print(
        "Artifacts:",
        PROBE_DIR.relative_to(ROOT),
    )

    print()

    if passed:
        print(
            "✅ PASS: 100-second process-boundary "
            "checkpoint restored equivalently."
        )
        return 0

    print(
        "❌ FAIL: checkpoint continuation diverged "
        "from the continuous control."
    )

    return 1


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--child",
        choices=[
            "continuous",
            "chunk1",
            "chunk2",
        ],
    )

    parser.add_argument("--request")
    parser.add_argument("--output")
    parser.add_argument("--state")

    args = parser.parse_args()

    if args.child:
        return run_child(
            args.child,
            Path(args.request).resolve(),
            Path(args.output).resolve(),
            Path(args.state).resolve(),
        )

    return compare()


if __name__ == "__main__":
    raise SystemExit(main())
