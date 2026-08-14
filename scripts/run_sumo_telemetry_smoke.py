"""Run a protected 20- or 30-minute regional telemetry smoke from a prior run."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from backend.app.core.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-minutes", type=int, choices=(20, 30), default=30)
    parser.add_argument(
        "--source-run",
        type=Path,
        help="Optional completed regional run directory; defaults to the newest usable run",
    )
    args = parser.parse_args()
    settings = Settings()
    source_run = (
        args.source_run.resolve()
        if args.source_run is not None
        else _latest_completed_regional_run(settings.sumo_runs_path.resolve())
    )
    worker_request = json.loads(
        (source_run / "request.json").read_text(encoding="utf-8")
    )
    if worker_request.get("run_kind") != "regional_comparison":
        raise ValueError("Telemetry smoke source must be a regional comparison")
    payload = dict(worker_request["request"])
    payload.update(
        {
            "warmup_minutes": 0,
            "analysis_minutes": int(args.analysis_minutes),
            "real_vehicles_per_simulated_vehicle": 1.0,
            "edge_telemetry_interval_seconds": 900,
        }
    )
    worker_request["request"] = payload
    worker_request["compute_chunk_seconds"] = int(
        worker_request.get("compute_chunk_seconds", settings.sumo_compute_chunk_seconds)
    )
    worker_request.pop("application_run_id", None)
    worker_request.pop("run_identity", None)
    run_identity = hashlib.sha256(
        json.dumps(worker_request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    application_run_id = str(uuid4())
    worker_request["application_run_id"] = application_run_id
    worker_request["run_identity"] = run_identity
    run_dir = settings.sumo_runs_path.resolve() / application_run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    request_path = run_dir / "request.json"
    request_path.write_text(
        json.dumps(worker_request, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "backend.app.workers.sumo_worker",
        "--request",
        str(request_path),
    ]
    print(f"Source run: {source_run.name}")
    print(f"Telemetry smoke run: {application_run_id}")
    print(f"Run directory: {run_dir}")
    completed = subprocess.run(command, cwd=Path.cwd(), check=False)
    result_path = run_dir / "result.json"
    if completed.returncode != 0 or not result_path.is_file():
        print("Telemetry smoke failed; inspect the run directory logs/artifacts.")
        return 1
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    print("Regional result and playback completed.")
    print(
        "Inspect with:\n"
        ".venv/bin/python -m scripts.inspect_sumo_edge_telemetry --latest"
    )
    return 0


def _latest_completed_regional_run(runs_root: Path) -> Path:
    required_worker_fields = {
        "network_path",
        "network_manifest_path",
        "edge_map_path",
        "gateway_connector_path",
        "graph_manifest_path",
        "nodes_path",
        "background_seed_directory",
        "demand_cache_directory",
        "traffic_schedule_path",
        "traffic_schedule_manifest_path",
    }
    candidates = sorted(
        (path.parent for path in runs_root.glob("*/result.json")),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for run_dir in candidates:
        request_path = run_dir / "request.json"
        result_path = run_dir / "result.json"
        if not request_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            result.get("status") == "completed"
            and request.get("run_kind") == "regional_comparison"
            and required_worker_fields.issubset(request)
        ):
            return run_dir
    raise FileNotFoundError(
        "No completed regional run can seed the smoke; run one normal comparison first"
    )


if __name__ == "__main__":
    raise SystemExit(main())
