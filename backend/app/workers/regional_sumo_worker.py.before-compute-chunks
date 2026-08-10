"""Run a bounded regional baseline and arbitrary-closure comparison in SUMO."""

from __future__ import annotations

import argparse
import gc
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from backend.app.services.sumo.demand_service import build_sumo_demand
from backend.app.services.sumo.output_service import parse_tripinfo
from backend.app.services.sumo.proxy_od_service import compile_proxy_od_snapshot
from backend.app.services.traffic_schedule_service import TrafficScheduleService

VEHICLE_CLASSES = ["passenger", "delivery", "truck", "bus"]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def run_regional(request_path: Path, worker: dict[str, Any]) -> int:
    run_dir = request_path.parent.resolve()
    repository = Path.cwd().resolve()
    input_paths = {
        name: Path(worker[name]).resolve()
        for name in (
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
        )
    }
    if not _inside(run_dir, repository) or any(
        not _inside(path, repository) for path in input_paths.values()
    ):
        raise ValueError("Regional worker inputs must remain inside the local repository")

    payload = dict(worker["request"])
    departure = datetime.fromisoformat(payload["departure_time"])
    warmup_minutes = int(payload["warmup_minutes"])
    analysis_minutes = int(payload["analysis_minutes"])
    total_minutes = warmup_minutes + analysis_minutes
    seed = int(payload["seed"])
    scale = float(payload["real_vehicles_per_simulated_vehicle"])
    progress_path = run_dir / "progress.json"
    result_path = run_dir / "result.json"
    cancel_path = run_dir / "cancel.requested"

    schedule = TrafficScheduleService(
        input_paths["traffic_schedule_path"],
        input_paths["traffic_schedule_manifest_path"],
    )
    schedule.load()
    source_dir = run_dir / "proxy-source"
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "start": (departure - timedelta(minutes=warmup_minutes)).isoformat(),
                "duration_minutes": total_minutes,
                "scale": scale,
                "seed": seed,
                "network_manifest": input_paths[
                    "network_manifest_path"
                ].read_text(encoding="utf-8"),
                "schedule_manifest": input_paths[
                    "traffic_schedule_manifest_path"
                ].read_text(encoding="utf-8"),
                "background_seed_report": (
                    input_paths[
                        "background_seed_directory"
                    ]
                    / "validation-report.json"
                ).read_text(encoding="utf-8"),
                "background_seed_od_sha256": hashlib.sha256(
                    (
                        input_paths[
                            "background_seed_directory"
                        ]
                        / "od-demand-seeds.parquet"
                    ).read_bytes()
                ).hexdigest(),
                "gateway_connector_sha256": hashlib.sha256(
                    input_paths["gateway_connector_path"].read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    cache_root = input_paths["demand_cache_directory"]
    cache_root.mkdir(parents=True, exist_ok=True)
    demand_dir = cache_root / cache_key
    _atomic_json(progress_path, {"status": "running", "stage": "demand", "progress": 0.01})
    manifest_path = demand_dir / "demand-manifest.json"
    cached_manifest = _cached_demand_manifest(manifest_path, demand_dir)
    if cached_manifest is not None:
        demand_manifest = cached_manifest
        _atomic_json(progress_path, {"status": "running", "stage": "demand_cache_hit", "progress": 0.08})
    else:
        compile_proxy_od_snapshot(
            background_seed_directory=input_paths["background_seed_directory"],
            nodes_path=input_paths["nodes_path"],
            graph_manifest_path=input_paths["graph_manifest_path"],
            schedule_service=schedule,
            departure_time=departure - timedelta(minutes=warmup_minutes),
            duration_minutes=total_minutes,
            output_directory=source_dir,
            source_version=f"regional-run-{seed}",
            log=lambda _message: None,
        )
        staging_demand = run_dir / "demand-staging"
        demand_manifest = build_sumo_demand(
            intake_directory=source_dir,
            network_path=input_paths["network_path"],
            network_manifest_path=input_paths["network_manifest_path"],
            edge_map_path=input_paths["edge_map_path"],
            output_directory=staging_demand,
            demand_version=f"runtime-{cache_key}",
            period="proxy_snapshot",
            start_seconds=0,
            sampling_scale=scale,
            seed=seed,
            gateway_connector_path=input_paths["gateway_connector_path"],
            log=lambda _message: None,
        )
        try:
            os.replace(staging_demand, demand_dir)
        except OSError:
            if not manifest_path.is_file():
                raise
        demand_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Finish all Pandas/PyArrow work BEFORE importing libsumo.
    #
    # Keeping Arrow-backed DataFrames alive after libsumo starts has caused
    # extremely expensive native memory cleanup pauses on macOS. Convert the
    # edge mapping into ordinary Python structures, release the DataFrame,
    # and force cleanup before SUMO begins.
    edge_map = pd.read_parquet(
        input_paths["edge_map_path"],
        columns=["app_edge_id", "sumo_edge_id", "status", "road_name"],
    )

    app_edge_ids = {
        payload["origin_app_edge_id"],
        payload["destination_app_edge_id"],
        *(
            edge_id
            for closure in payload["closures"]
            for edge_id in closure["app_edge_ids"]
        ),
    }

    mapping = _accepted_mapping(edge_map, app_edge_ids)

    edge_names = {
        str(row.sumo_edge_id): str(row.road_name or row.sumo_edge_id)
        for row in edge_map.dropna(subset=["sumo_edge_id"]).itertuples()
    }

    # From this point forward the SUMO worker only needs the plain-Python
    # mapping/edge_names structures, not the Pandas/PyArrow DataFrame.
    del edge_map
    gc.collect()

    # The regional orchestrator deliberately does NOT import libsumo.
    # Each SUMO variant runs in its own child Python process so exiting the
    # child forces macOS to reclaim all libsumo/native allocator memory.

    if cancel_path.exists():
        _atomic_json(result_path, {"status": "cancelled", "seed": seed})
        return 0

    overall_deadline = (
        time.monotonic()
        + int(worker["max_run_seconds"])
    )

    common = {
        "network_path": input_paths["network_path"],
        "route_path": demand_dir / "regional.rou.xml.gz",
        "sumo_binary": str(worker["sumo_binary"]),
        "mapping": mapping,
        "payload": payload,
        "run_dir": run_dir,
        "cancel_path": cancel_path,
        "progress_path": progress_path,
        "max_visible": int(worker["max_visible_vehicles"]),
    }
    # A baseline depends on the network, demand, selected trip, seed and
    # physical-model settings, but NOT on the closure definition. Reuse an
    # identical completed baseline instead of running SUMO again.
    baseline_cache_allowed = not bool(
        os.getenv("COMMUTE_HELP_SUMO_PROFILE_SECONDS", "").strip()
    )

    baseline_cache_root = (
        input_paths["demand_cache_directory"].parent
        / "baseline-cache"
    )
    baseline_cache_root.mkdir(parents=True, exist_ok=True)

    baseline_cache_key = _baseline_cache_key(
        payload=payload,
        worker=worker,
        mapping=mapping,
        input_paths=input_paths,
        demand_manifest=demand_manifest,
    )
    baseline_cache_path = (
        baseline_cache_root
        / f"{baseline_cache_key}.json.gz"
    )

    baseline = (
        _load_baseline_cache(
            baseline_cache_path,
            baseline_cache_key,
        )
        if baseline_cache_allowed
        else None
    )

    baseline_cache_hit = baseline is not None

    if baseline_cache_hit:
        _atomic_json(
            progress_path,
            {
                "status": "running",
                "stage": "baseline_cache_hit",
                "sim_second": total_minutes * 60,
                "progress": 0.52,
            },
        )
    else:
        baseline = _run_variant_process(
            name="baseline",
            closures=[],
            progress_start=0.10,
            progress_span=0.42,
            max_seconds=_remaining_run_seconds(
                overall_deadline
            ),
            **common,
        )

    if baseline["status"] == "cancelled":
        _atomic_json(
            result_path,
            {"status": "cancelled", "seed": seed},
        )
        return 0
    scenario = _run_variant_process(
        name="scenario",
        closures=payload["closures"],
        progress_start=0.52,
        progress_span=0.47,
        max_seconds=_remaining_run_seconds(
            overall_deadline
        ),
        **common,
    )
    if scenario["status"] == "cancelled":
        _atomic_json(result_path, {"status": "cancelled", "seed": seed})
        return 0

    # Validate SUMO output against SUMO's own free-flow route rather
    # than the app graph's node-to-node floor. The selected SUMO trip
    # begins/ends on mapped SUMO edges, so the NetworkX floor is useful
    # as a diagnostic but is not an apples-to-apples hard rejection.
    graph_floor_seconds = float(worker["free_flow_floor_seconds"])

    validation: dict[str, dict[str, float | bool]] = {}

    for label, variant in (
        ("baseline", baseline),
        ("scenario", scenario),
    ):
        trip = variant.get("selected_trip")
        sumo_floor_seconds = float(
            variant["selected_free_flow_seconds"]
        )

        tolerance_seconds = max(
            5.0,
            sumo_floor_seconds * 0.03,
        )
        minimum_allowed_seconds = max(
            0.1,
            sumo_floor_seconds - tolerance_seconds,
        )

        valid = (
            trip is None
            or float(trip["duration"])
            >= minimum_allowed_seconds
        )

        validation[label] = {
            "sumo_free_flow_seconds": round(
                sumo_floor_seconds,
                3,
            ),
            "minimum_allowed_seconds": round(
                minimum_allowed_seconds,
                3,
            ),
            "valid": valid,
        }

        if not valid:
            raise ValueError(
                f"{label} selected trip violated the "
                f"SUMO-native physical free-flow floor"
            )

    if (
        baseline_cache_allowed
        and not baseline_cache_hit
    ):
        _write_baseline_cache(
            baseline_cache_path,
            baseline_cache_key,
            baseline,
        )

    # Baseline playback is intentionally not recorded. Scenario frames
    # were spilled to bounded gzip chunks during simulation. Assemble the
    # compatibility playback.json by streaming those chunks directly to
    # disk rather than rebuilding all frames in memory.
    baseline.pop("frame_chunks", None)
    scenario_frame_chunks = [
        run_dir / relative_path
        for relative_path in scenario.pop(
            "frame_chunks",
            [],
        )
    ]

    _write_playback_from_chunks(
        run_dir / "playback.json",
        {
            "schema_version": 1,
            "duration_seconds": analysis_minutes * 60,
            "frame_interval_seconds": int(
                payload["frame_interval_seconds"]
            ),
            "seed": seed,
            "real_vehicles_per_simulated_vehicle": scale,
            "displayed_vehicle_limit": int(
                worker["max_visible_vehicles"]
            ),
        },
        scenario_frame_chunks,
    )
    edge_changes = _edge_changes(
        edge_names, baseline.pop("edge_stats"), scenario.pop("edge_stats")
    )
    result = {
        "status": "completed",
        "evidence_level": "modeled_uncalibrated",
        "seed": seed,
        "baseline_cache_hit": baseline_cache_hit,
        "baseline_cache_key": baseline_cache_key,
        "demand_version": demand_manifest["demand_version"],
        "demand_model_version": demand_manifest["source"]["model"]["version"],
        "real_vehicles_per_simulated_vehicle": scale,
        "represented_real_vehicle_trips": demand_manifest["sampling"][
            "represented_real_vehicle_trips"
        ],
        "baseline": baseline,
        "scenario": scenario,
        "comparison": {
            "selected_trip_delta_seconds": _trip_delta(baseline.get("selected_trip"), scenario.get("selected_trip")),
            "teleport_delta": scenario["teleport_count"] - baseline["teleport_count"],
            "arrived_vehicle_delta": scenario["arrived_vehicle_count"] - baseline["arrived_vehicle_count"],
            "edge_changes": edge_changes[:300],
        },
        "free_flow_validation": {
            "graph_reference_free_flow_seconds": round(
                graph_floor_seconds,
                3,
            ),
            "baseline_sumo_free_flow_seconds": validation[
                "baseline"
            ]["sumo_free_flow_seconds"],
            "scenario_sumo_free_flow_seconds": validation[
                "scenario"
            ]["sumo_free_flow_seconds"],
            "baseline_minimum_allowed_seconds": validation[
                "baseline"
            ]["minimum_allowed_seconds"],
            "scenario_minimum_allowed_seconds": validation[
                "scenario"
            ]["minimum_allowed_seconds"],
            "baseline_valid": validation[
                "baseline"
            ]["valid"],
            "scenario_valid": validation[
                "scenario"
            ]["valid"],
        },
        "playback_available": True,
        "assumptions": [
            "Regional demand uses the local detector-fitted proxy OD model, not an observed regional trip table.",
            "The baseline and closure runs use identical demand, sampling scale, and seed.",
            "This regional run is mesoscopic; detailed signal phases and lane-changing near the closure require the affected-area microscopic phase.",
        ],
    }
    _atomic_json(result_path, result)
    _atomic_json(progress_path, {"status": "completed", "progress": 1.0, "sim_second": total_minutes * 60})
    return 0



BASELINE_CACHE_SCHEMA_VERSION = 1


def _baseline_cache_key(
    *,
    payload: dict[str, Any],
    worker: dict[str, Any],
    mapping: dict[str, list[str]],
    input_paths: dict[str, Path],
    demand_manifest: dict[str, Any],
) -> str:
    """Return a deterministic key for closure-independent baseline state."""

    origin_id = str(payload["origin_app_edge_id"])
    destination_id = str(payload["destination_app_edge_id"])

    sumo_binary = Path(str(worker["sumo_binary"])).resolve()
    try:
        sumo_stat = sumo_binary.stat()
        sumo_binary_identity = {
            "path": str(sumo_binary),
            "size": sumo_stat.st_size,
            "mtime_ns": sumo_stat.st_mtime_ns,
        }
    except OSError:
        sumo_binary_identity = {
            "path": str(sumo_binary),
            "size": None,
            "mtime_ns": None,
        }

    source = {
        "schema_version": BASELINE_CACHE_SCHEMA_VERSION,

        # Any worker-code change automatically invalidates old baselines.
        "regional_worker_sha256": _sha256(
            Path(__file__).resolve()
        ),

        "network_manifest_sha256": _sha256(
            input_paths["network_manifest_path"]
        ),
        "graph_manifest_sha256": _sha256(
            input_paths["graph_manifest_path"]
        ),

        "sumo_binary": sumo_binary_identity,

        "demand_version": demand_manifest["demand_version"],
        "demand_route_sha256": demand_manifest[
            "artifacts"
        ]["routes"]["sha256"],

        "departure_time": payload["departure_time"],

        "origin_app_edge_id": origin_id,
        "destination_app_edge_id": destination_id,
        "origin_sumo_edges": mapping[origin_id],
        "destination_sumo_edges": mapping[destination_id],

        "warmup_minutes": int(payload["warmup_minutes"]),
        "analysis_minutes": int(payload["analysis_minutes"]),
        "real_vehicles_per_simulated_vehicle": float(
            payload["real_vehicles_per_simulated_vehicle"]
        ),
        "seed": int(payload["seed"]),

        "reroute_period_seconds": int(
            payload["reroute_period_seconds"]
        ),
        "aggregate_interval_seconds": int(
            payload["aggregate_interval_seconds"]
        ),
        "frame_interval_seconds": int(
            payload["frame_interval_seconds"]
        ),
    }

    return hashlib.sha256(
        json.dumps(
            source,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]


def _load_baseline_cache(
    path: Path,
    expected_key: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None

    try:
        with gzip.open(
            path,
            "rt",
            encoding="utf-8",
        ) as source:
            cached = json.load(source)

        if (
            cached.get("schema_version")
            != BASELINE_CACHE_SCHEMA_VERSION
        ):
            return None

        if cached.get("cache_key") != expected_key:
            return None

        baseline = cached.get("baseline")

        if (
            not isinstance(baseline, dict)
            or baseline.get("status") != "completed"
            or "edge_stats" not in baseline
            or "selected_free_flow_seconds" not in baseline
        ):
            return None

        return baseline

    except (
        OSError,
        EOFError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def _write_baseline_cache(
    path: Path,
    cache_key: str,
    baseline: dict[str, Any],
) -> None:
    """Persist a validated baseline atomically as compressed JSON."""

    temporary = path.with_suffix(
        path.suffix + ".part"
    )

    value = {
        "schema_version": BASELINE_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "baseline": baseline,
    }

    with gzip.open(
        temporary,
        "wt",
        encoding="utf-8",
        compresslevel=1,
    ) as output:
        json.dump(
            value,
            output,
            separators=(",", ":"),
        )

    os.replace(temporary, path)


def _accepted_mapping(edge_map: pd.DataFrame, app_edge_ids: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    blocked: list[str] = []
    for app_edge_id in sorted(app_edge_ids):
        rows = edge_map.loc[edge_map["app_edge_id"].astype(str) == str(app_edge_id)]
        statuses = set(rows["status"].astype(str))
        ids = sorted(
            str(value)
            for value in rows.loc[rows["status"] == "accepted", "sumo_edge_id"].dropna().unique()
        )
        if statuses != {"accepted"} or not ids:
            blocked.append(str(app_edge_id))
        else:
            result[str(app_edge_id)] = ids
    if blocked:
        raise ValueError("Closure simulation blocked by unaccepted edge mappings: " + ", ".join(blocked))
    return result


def _cached_demand_manifest(
    manifest_path: Path, demand_directory: Path
) -> dict[str, Any] | None:
    route_path = demand_directory / "regional.rou.xml.gz"
    if not manifest_path.is_file() or not route_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["artifacts"]["routes"]["sha256"] != _sha256(route_path):
            return None
        if manifest["sampling"]["gate_passed"] is not True:
            return None
        return manifest
    except (KeyError, OSError, json.JSONDecodeError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _remaining_run_seconds(
    deadline: float,
) -> int:
    remaining = int(deadline - time.monotonic())

    if remaining < 1:
        raise TimeoutError(
            "Regional SUMO comparison exceeded its "
            "maximum wall-clock runtime"
        )

    return remaining


def _write_variant_result(
    path: Path,
    value: dict[str, Any],
) -> None:
    temporary = path.with_suffix(
        path.suffix + ".part"
    )

    with gzip.open(
        temporary,
        "wt",
        encoding="utf-8",
        compresslevel=1,
    ) as output:
        json.dump(
            value,
            output,
            separators=(",", ":"),
        )

    os.replace(temporary, path)


def _read_variant_result(
    path: Path,
) -> dict[str, Any]:
    with gzip.open(
        path,
        "rt",
        encoding="utf-8",
    ) as source:
        result = json.load(source)

    if not isinstance(result, dict):
        raise ValueError(
            f"Invalid SUMO variant result: {path}"
        )

    return result


def _run_variant_process(
    *,
    name: str,
    closures: list[dict[str, Any]],
    network_path: Path,
    route_path: Path,
    sumo_binary: str,
    mapping: dict[str, list[str]],
    payload: dict[str, Any],
    run_dir: Path,
    cancel_path: Path,
    progress_path: Path,
    max_visible: int,
    progress_start: float,
    progress_span: float,
    max_seconds: int,
) -> dict[str, Any]:
    """Run one SUMO variant in a disposable Python process."""

    child_request_path = (
        run_dir / f"{name}-variant-request.json"
    )
    child_result_path = (
        run_dir / f"{name}-variant-result.json.gz"
    )
    child_stdout_path = (
        run_dir / f"{name}-variant.stdout.log"
    )
    child_stderr_path = (
        run_dir / f"{name}-variant.stderr.log"
    )

    if child_result_path.exists():
        child_result_path.unlink()

    child_request = {
        "name": name,
        "closures": closures,
        "network_path": str(network_path),
        "route_path": str(route_path),
        "sumo_binary": sumo_binary,
        "mapping": mapping,
        "payload": payload,
        "run_dir": str(run_dir),
        "cancel_path": str(cancel_path),
        "progress_path": str(progress_path),
        "max_visible": int(max_visible),
        "progress_start": float(progress_start),
        "progress_span": float(progress_span),
        "max_seconds": int(max_seconds),
        "result_path": str(child_result_path),
    }

    _atomic_json(
        child_request_path,
        child_request,
    )

    command = [
        sys.executable,
        "-m",
        "backend.app.workers.regional_sumo_worker",
        "--variant-request",
        str(child_request_path),
    ]

    try:
        with (
            child_stdout_path.open(
                "w",
                encoding="utf-8",
            ) as stdout,
            child_stderr_path.open(
                "w",
                encoding="utf-8",
            ) as stderr,
        ):
            completed = subprocess.run(
                command,
                cwd=Path.cwd(),
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=max_seconds + 120,
                check=False,
            )

    except subprocess.TimeoutExpired as error:
        raise TimeoutError(
            f"{name} SUMO child exceeded its "
            "wall-clock runtime"
        ) from error

    if not child_result_path.is_file():
        detail = ""

        try:
            stderr_text = child_stderr_path.read_text(
                encoding="utf-8"
            )
            detail = stderr_text[-4000:].strip()
        except OSError:
            pass

        raise RuntimeError(
            f"{name} SUMO child exited with code "
            f"{completed.returncode} without producing "
            f"a result"
            + (f": {detail}" if detail else "")
        )

    result = _read_variant_result(
        child_result_path
    )

    if result.get("status") == "failed":
        raise RuntimeError(
            str(
                result.get(
                    "error",
                    f"{name} SUMO child failed",
                )
            )
        )

    if completed.returncode != 0:
        raise RuntimeError(
            f"{name} SUMO child exited with code "
            f"{completed.returncode}"
        )

    return result


def _run_variant_child(
    request_path: Path,
) -> int:
    """Entry point for one disposable libsumo process."""

    request = json.loads(
        request_path.read_text(
            encoding="utf-8"
        )
    )

    result_path = Path(
        request["result_path"]
    ).resolve()

    try:
        run_dir = Path(
            request["run_dir"]
        ).resolve()

        if not _inside(
            run_dir,
            Path.cwd().resolve(),
        ):
            raise ValueError(
                "Variant run directory must remain "
                "inside the local repository"
            )

        # Import libsumo ONLY in this child process.
        global libsumo
        import libsumo

        result = _run_variant(
            name=str(request["name"]),
            closures=list(request["closures"]),
            network_path=Path(
                request["network_path"]
            ).resolve(),
            route_path=Path(
                request["route_path"]
            ).resolve(),
            sumo_binary=str(
                request["sumo_binary"]
            ),
            mapping={
                str(key): [
                    str(value)
                    for value in values
                ]
                for key, values
                in request["mapping"].items()
            },
            payload=dict(request["payload"]),
            run_dir=run_dir,
            cancel_path=Path(
                request["cancel_path"]
            ).resolve(),
            progress_path=Path(
                request["progress_path"]
            ).resolve(),
            max_visible=int(
                request["max_visible"]
            ),
            deadline=(
                time.monotonic()
                + int(request["max_seconds"])
            ),
            progress_start=float(
                request["progress_start"]
            ),
            progress_span=float(
                request["progress_span"]
            ),
        )

        _write_variant_result(
            result_path,
            result,
        )

        return 0

    except Exception as error:
        traceback.print_exc()

        try:
            _write_variant_result(
                result_path,
                {
                    "status": "failed",
                    "error": str(error),
                },
            )
        except Exception:
            traceback.print_exc()

        return 1


def _run_variant(
    *,
    name: str,
    closures: list[dict[str, Any]],
    network_path: Path,
    route_path: Path,
    sumo_binary: str,
    mapping: dict[str, list[str]],
    payload: dict[str, Any],
    run_dir: Path,
    cancel_path: Path,
    progress_path: Path,
    max_visible: int,
    deadline: float,
    progress_start: float,
    progress_span: float,
) -> dict[str, Any]:
    warmup_seconds = int(payload["warmup_minutes"]) * 60
    analysis_seconds = int(payload["analysis_minutes"]) * 60
    requested_end_second = warmup_seconds + analysis_seconds

    profile_raw = os.getenv("COMMUTE_HELP_SUMO_PROFILE_SECONDS", "").strip()
    profile_seconds = int(profile_raw) if profile_raw else 0
    profiling_enabled = profile_seconds > 0

    end_second = (
        min(requested_end_second, profile_seconds)
        if profiling_enabled
        else requested_end_second
    )

    timing_path = run_dir / f"{name}-timings.jsonl"
    timing_buffer: list[dict[str, Any]] = []

    if profiling_enabled and timing_path.exists():
        timing_path.unlink()

    def flush_timings() -> None:
        if not profiling_enabled or not timing_buffer:
            return

        with timing_path.open("a", encoding="utf-8") as output:
            for record in timing_buffer:
                output.write(json.dumps(record, separators=(",", ":")) + "\n")

        timing_buffer.clear()

    tripinfo_path = run_dir / f"{name}-tripinfo.xml"
    # Full and lane closures are modeled with SUMO's native rerouter.
    # Speed restrictions remain TraCI-managed because they do not invalidate
    # route connectivity.
    native_closures = [
        closure
        for closure in closures
        if closure["restriction_type"] in {"full", "lane"}
    ]
    speed_closures = [
        closure
        for closure in closures
        if closure["restriction_type"] == "speed"
    ]

    rerouter_path = None

    if native_closures:
        rerouter_path = _write_native_closure_rerouter(
            run_dir=run_dir,
            name=name,
            network_path=network_path,
            route_path=route_path,
            closures=native_closures,
            mapping=mapping,
            departure_iso=payload["departure_time"],
            warmup_seconds=warmup_seconds,
            end_second=end_second,
        )

    command = [
        sumo_binary,
        "--net-file", str(network_path),
        "--route-files", str(route_path),
        "--tripinfo-output", str(tripinfo_path),
        "--begin", "0",
        "--end", str(end_second),
        "--seed", str(payload["seed"]),
        "--mesosim", "true",
        "--routing-algorithm", "astar",
        "--device.rerouting.probability", "1",
        "--device.rerouting.mode", "8",
        "--device.rerouting.period", str(payload["reroute_period_seconds"]),
        "--device.rerouting.adaptation-steps", "6",
        "--device.rerouting.adaptation-interval", "10",
        "--device.rerouting.threads", "6",
        "--time-to-teleport", "300",
        "--no-step-log", "true",
        "--no-warnings", "true",
    ]

    if rerouter_path is not None:
        command.extend(
            ["--additional-files", str(rerouter_path)]
        )

    libsumo.start(command)

    selected_id = "selected-trip"
    selected_initial_route: list[str] = []
    selected_final_route: list[str] = []
    last_selected_route: list[str] = []
    # Playback is useful only for the scenario variant. Keep at most
    # one 60-second playback chunk in memory and spill completed chunks
    # to disk immediately.
    playback_enabled = name == "scenario"
    playback_chunk_seconds = 60
    playback_dir = run_dir / f"{name}-playback"

    if playback_enabled:
        playback_dir.mkdir(parents=True, exist_ok=True)

    frame_buffer: list[dict[str, Any]] = []
    frame_chunk_paths: list[Path] = []
    frame_chunk_index: int | None = None

    def flush_frame_buffer() -> None:
        nonlocal frame_buffer

        if not frame_buffer:
            return

        if frame_chunk_index is None:
            raise RuntimeError(
                "Playback frame buffer has no chunk index"
            )

        chunk_path = playback_dir / (
            f"chunk-{frame_chunk_index:05d}.jsonl.gz"
        )

        _write_playback_chunk(
            chunk_path,
            frame_buffer,
        )

        frame_chunk_paths.append(chunk_path)

        # Release all frame/agent dictionaries from this temporal window.
        frame_buffer.clear()

    traveled: list[list[float]] = []
    edge_totals: dict[str, dict[str, Any]] = {}
    departed = 0
    arrived = 0
    teleports = 0
    restriction_state: dict[int, bool] = {}
    originals: dict[str, tuple[list[str], float]] = {}
    last_projected: list[list[float]] = []

    try:
        route, selected_free_flow_seconds = _find_selected_route(
            mapping[str(payload["origin_app_edge_id"])],
            mapping[str(payload["destination_app_edge_id"])],
        )
        selected_initial_route = route

        libsumo.route.add(f"{name}-selected-route", route)
        libsumo.vehicle.add(
            selected_id,
            f"{name}-selected-route",
            typeID="passenger_sov",
            depart=str(warmup_seconds),
        )

        for sim_second in range(end_second + 1):
            if cancel_path.exists() or time.monotonic() > deadline:
                flush_timings()
                return {
                    "status": "cancelled",
                    "frames": [],
                    "edge_stats": {},
                }

            loop_started = time.perf_counter()

            # ---------------------------------------------------------
            # SUMO simulation step
            # ---------------------------------------------------------
            started = time.perf_counter()
            libsumo.simulationStep()
            step_ms = (time.perf_counter() - started) * 1000.0

            now = int(round(float(libsumo.simulation.getTime())))

            # ---------------------------------------------------------
            # Basic SUMO bookkeeping
            # ---------------------------------------------------------
            started = time.perf_counter()

            departed += int(
                libsumo.simulation.getDepartedNumber()
            )
            arrived += int(
                libsumo.simulation.getArrivedNumber()
            )
            teleports += len(
                libsumo.simulation.getStartingTeleportIDList()
            )

            bookkeeping_ms = (
                time.perf_counter() - started
            ) * 1000.0

            # ---------------------------------------------------------
            # Dynamic speed-restriction synchronization
            #
            # Full closures and lane reductions are handled by the
            # native SUMO rerouter loaded as an additional file.
            # ---------------------------------------------------------
            restriction_ms = 0.0
            forced_reroute_ms = 0.0
            restriction_changed = False

            if speed_closures:
                started = time.perf_counter()

                restriction_changed = _sync_restrictions(
                    speed_closures,
                    mapping,
                    payload["departure_time"],
                    warmup_seconds,
                    now,
                    restriction_state,
                    originals,
                )

                restriction_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                if restriction_changed:
                    started = time.perf_counter()
                    _reroute_active_vehicles()
                    forced_reroute_ms = (
                        time.perf_counter() - started
                    ) * 1000.0

            # ---------------------------------------------------------
            # Active vehicle list
            # ---------------------------------------------------------
            started = time.perf_counter()

            active = list(libsumo.vehicle.getIDList())

            vehicle_list_ms = (
                time.perf_counter() - started
            ) * 1000.0

            # ---------------------------------------------------------
            # Playback / edge capture
            # ---------------------------------------------------------
            started = time.perf_counter()

            if (
                now >= warmup_seconds
                and now % int(payload["aggregate_interval_seconds"]) == 0
            ):
                _capture_edge_stats(active, edge_totals)

            if (
                now >= warmup_seconds
                and now % int(payload["frame_interval_seconds"]) == 0
            ):
                elapsed_seconds = now - warmup_seconds

                if playback_enabled:
                    next_chunk_index = (
                        elapsed_seconds
                        // playback_chunk_seconds
                    )

                    if (
                        frame_buffer
                        and frame_chunk_index is not None
                        and next_chunk_index != frame_chunk_index
                    ):
                        flush_frame_buffer()

                    frame_chunk_index = next_chunk_index

                    frame, last_projected = _capture_frame(
                        active,
                        selected_id,
                        max_visible,
                        traveled,
                        last_projected,
                        elapsed_seconds,
                    )

                    frame_buffer.append(frame)

                    if frame["selected_route_edges"]:
                        last_selected_route = list(
                            frame["selected_route_edges"]
                        )

                # Baseline playback is never presented to the user, so do
                # not build hundreds of agent dictionaries for it. We still
                # retain the selected vehicle's latest route for metrics.
                elif selected_id in set(active):
                    try:
                        last_selected_route = list(
                            libsumo.vehicle.getRoute(selected_id)
                        )
                    except libsumo.TraCIException:
                        pass

            capture_ms = (
                time.perf_counter() - started
            ) * 1000.0

            # ---------------------------------------------------------
            # Existing progress reporting
            # ---------------------------------------------------------
            progress = (
                progress_start
                + progress_span
                * min(now / max(end_second, 1), 1)
            )

            if now % 10 == 0:
                _atomic_json(
                    progress_path,
                    {
                        "status": "running",
                        "stage": name,
                        "sim_second": now,
                        "progress": progress,
                    },
                )

            # Calculate this AFTER normal worker processing but BEFORE
            # profiling output so profiler file I/O is excluded.
            total_ms = (
                time.perf_counter() - loop_started
            ) * 1000.0

            if profiling_enabled:
                timing_buffer.append(
                    {
                        "sim_second": now,
                        "active_vehicles": len(active),
                        "step_ms": round(step_ms, 3),
                        "bookkeeping_ms": round(bookkeeping_ms, 3),
                        "restriction_ms": round(restriction_ms, 3),
                        "forced_reroute_ms": round(
                            forced_reroute_ms, 3
                        ),
                        "vehicle_list_ms": round(
                            vehicle_list_ms, 3
                        ),
                        "capture_ms": round(capture_ms, 3),
                        "total_ms": round(total_ms, 3),
                        "restriction_changed": restriction_changed,
                    }
                )

                if len(timing_buffer) >= 10:
                    flush_timings()

        if selected_id in set(libsumo.vehicle.getIDList()):
            selected_final_route = list(
                libsumo.vehicle.getRoute(selected_id)
            )
        elif last_selected_route:
            selected_final_route = last_selected_route

    finally:
        flush_frame_buffer()
        flush_timings()
        libsumo.close()

    trip = None

    try:
        trip = parse_tripinfo(tripinfo_path, selected_id)
    except (ValueError, OSError):
        pass

    return {
        "status": "completed",
        "selected_trip": trip,
        "selected_free_flow_seconds": round(
            float(selected_free_flow_seconds),
            3,
        ),
        "selected_initial_route_edge_count": len(
            selected_initial_route
        ),
        "selected_final_route_edge_count": len(
            selected_final_route
        ),
        "selected_initial_route_hash": _route_hash(
            selected_initial_route
        ),
        "selected_final_route_hash": _route_hash(
            selected_final_route
        ),
        "selected_trip_rerouted": bool(
            selected_final_route
            and selected_final_route != selected_initial_route
        ),
        "departed_vehicle_count": departed,
        "arrived_vehicle_count": arrived,
        "teleport_count": teleports,
        "remaining_vehicle_count": (
            int(libsumo.simulation.getMinExpectedNumber())
            if False
            else 0
        ),
        "frame_chunks": [
            str(chunk_path.relative_to(run_dir))
            for chunk_path in frame_chunk_paths
        ],
        "edge_stats": dict(edge_totals),
    }


def _write_playback_chunk(
    path: Path,
    frames: list[dict[str, Any]],
) -> None:
    """Write one bounded temporal playback chunk."""
    temporary = path.with_suffix(path.suffix + ".part")

    with gzip.open(
        temporary,
        "wt",
        encoding="utf-8",
        compresslevel=1,
    ) as handle:
        for frame in frames:
            handle.write(
                json.dumps(
                    frame,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")

    os.replace(temporary, path)


def _write_playback_from_chunks(
    path: Path,
    metadata: dict[str, Any],
    chunk_paths: list[Path],
) -> None:
    """Stream chunked frames into the legacy playback.json contract.

    This deliberately avoids constructing the complete frames list in
    Python memory. The compatibility file can later be replaced by a
    chunk-aware playback API without changing the simulation worker.
    """
    temporary = path.with_suffix(path.suffix + ".part")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as output:
        encoded_metadata = json.dumps(
            metadata,
            separators=(",", ":"),
        )

        # Remove the final } and append the frames array ourselves.
        output.write(encoded_metadata[:-1])
        output.write(',"frames":[')

        first_frame = True

        for chunk_path in chunk_paths:
            with gzip.open(
                chunk_path,
                "rt",
                encoding="utf-8",
            ) as source:
                for line in source:
                    frame_json = line.strip()

                    if not frame_json:
                        continue

                    if not first_frame:
                        output.write(",")

                    output.write(frame_json)
                    first_frame = False

        output.write("]}\n")

    os.replace(temporary, path)


def _find_selected_route(
    origins: list[str],
    destinations: list[str],
) -> tuple[list[str], float]:
    best_route: list[str] | None = None
    best_travel_time: float | None = None

    for origin in origins:
        for destination in destinations:
            stage = libsumo.simulation.findRoute(
                origin,
                destination,
                vType="passenger_sov",
            )

            if not stage.edges:
                continue

            travel_time = float(stage.travelTime)

            if (
                best_travel_time is None
                or travel_time < best_travel_time
            ):
                best_route = list(stage.edges)
                best_travel_time = travel_time

    if best_route is None or best_travel_time is None:
        raise ValueError(
            "SUMO could not route the selected trip between "
            "accepted edge mappings"
        )

    return best_route, best_travel_time


def _sync_restrictions(
    closures: list[dict[str, Any]],
    mapping: dict[str, list[str]],
    departure_iso: str,
    warmup_seconds: int,
    sim_second: int,
    state: dict[int, bool],
    originals: dict[str, tuple[list[str], float]],
) -> bool:
    departure = datetime.fromisoformat(departure_iso)
    simulation_start = departure - timedelta(seconds=warmup_seconds)
    changed = False
    for index, closure in enumerate(closures):
        starts = datetime.fromisoformat(closure["starts_at"]) if closure.get("starts_at") else None
        ends = datetime.fromisoformat(closure["ends_at"]) if closure.get("ends_at") else None
        active = starts is None or (simulation_start + timedelta(seconds=sim_second) >= starts and simulation_start + timedelta(seconds=sim_second) < ends)
        if state.get(index) == active:
            continue
        sumo_edges = [edge for app in closure["app_edge_ids"] for edge in mapping[str(app)]]
        if active:
            _apply_restriction(closure, sumo_edges, originals)
        else:
            _restore_restriction(sumo_edges, originals)
        state[index] = active
        changed = True
    return changed


def _apply_restriction(
    closure: dict[str, Any], edge_ids: list[str], originals: dict[str, tuple[list[str], float]]
) -> None:
    for edge_id in edge_ids:
        lane_count = libsumo.edge.getLaneNumber(edge_id)
        for lane_index in range(lane_count):
            lane_id = f"{edge_id}_{lane_index}"
            originals.setdefault(
                lane_id,
                (list(libsumo.lane.getAllowed(lane_id)), float(libsumo.lane.getMaxSpeed(lane_id))),
            )
            kind = closure["restriction_type"]
            if kind == "full" or kind == "lane" and lane_index >= int(closure["remaining_lanes"]):
                libsumo.lane.setDisallowed(lane_id, VEHICLE_CLASSES)
            elif kind == "speed":
                libsumo.lane.setMaxSpeed(lane_id, float(closure["speed_limit_kph"]) / 3.6)


def _restore_restriction(edge_ids: list[str], originals: dict[str, tuple[list[str], float]]) -> None:
    for edge_id in edge_ids:
        for lane_index in range(libsumo.edge.getLaneNumber(edge_id)):
            lane_id = f"{edge_id}_{lane_index}"
            original = originals.get(lane_id)
            if original is None:
                continue
            libsumo.lane.setAllowed(lane_id, original[0])
            libsumo.lane.setMaxSpeed(lane_id, original[1])


def _reroute_active_vehicles() -> None:
    for vehicle_id in libsumo.vehicle.getIDList():
        try:
            libsumo.vehicle.rerouteTraveltime(vehicle_id)
        except libsumo.TraCIException:
            continue



def _native_closure_window(
    closure: dict[str, Any],
    departure_iso: str,
    warmup_seconds: int,
    end_second: int,
) -> tuple[int, int]:
    """Translate an absolute closure window into SUMO seconds."""
    departure = datetime.fromisoformat(departure_iso)
    simulation_start = departure - timedelta(
        seconds=warmup_seconds
    )
    simulation_end = end_second + 1

    if closure.get("starts_at"):
        starts = datetime.fromisoformat(
            closure["starts_at"]
        )
        begin = int(
            max(
                0,
                (starts - simulation_start).total_seconds(),
            )
        )
    else:
        begin = 0

    if closure.get("ends_at"):
        ends = datetime.fromisoformat(
            closure["ends_at"]
        )
        end = int(
            min(
                simulation_end,
                (ends - simulation_start).total_seconds(),
            )
        )
    else:
        end = simulation_end

    return (
        max(0, min(begin, simulation_end)),
        max(0, min(end, simulation_end)),
    )


def _read_sumo_edge_metadata(
    network_path: Path,
) -> tuple[dict[str, float], dict[str, int]]:
    """Read only edge lengths and lane counts from the SUMO network."""
    lengths: dict[str, float] = {}
    lane_counts: dict[str, int] = {}

    for _, element in ET.iterparse(
        network_path,
        events=("end",),
    ):
        tag = element.tag.rsplit("}", 1)[-1]

        if tag != "edge":
            continue

        edge_id = element.attrib.get("id")

        if not edge_id or edge_id.startswith(":"):
            element.clear()
            continue

        lanes = [
            child
            for child in element
            if child.tag.rsplit("}", 1)[-1] == "lane"
        ]

        if lanes:
            lane_counts[edge_id] = len(lanes)

            try:
                lengths[edge_id] = float(
                    lanes[0].attrib.get("length", "0")
                )
            except ValueError:
                lengths[edge_id] = 0.0

        element.clear()

    return lengths, lane_counts


def _collect_closure_trigger_edges(
    route_path: Path,
    closure_edge_ids: set[str],
    edge_lengths: dict[str, float],
) -> list[str]:
    """Choose approach points for closure-aware rerouting.

    Rather than putting a rerouter on every road segment, choose a handful
    of approach points along demand routes that actually cross a closure.

    The farther markers matter for large regional closures where the useful
    diversion branch may be many kilometres upstream.
    """
    thresholds = (
        500.0,
        3000.0,
        8000.0,
        15000.0,
        25000.0,
    )

    triggers: set[str] = set()

    route_source = (
        gzip.open(route_path, "rb")
        if route_path.suffix == ".gz"
        else route_path.open("rb")
    )

    with route_source as route_stream:
        for _, element in ET.iterparse(
            route_stream,
            events=("end",),
        ):
            tag = element.tag.rsplit("}", 1)[-1]

            if tag != "route":
                continue

            raw_edges = element.attrib.get("edges", "")
            route_edges = raw_edges.split()

            if not route_edges:
                element.clear()
                continue

            closure_positions = [
                index
                for index, edge_id in enumerate(route_edges)
                if edge_id in closure_edge_ids
            ]

            if not closure_positions:
                element.clear()
                continue

            # A planned closure should also be known from departure.
            triggers.add(route_edges[0])

            for closure_index in closure_positions:
                distance = 0.0
                threshold_index = 0

                for index in range(
                    closure_index - 1,
                    -1,
                    -1,
                ):
                    edge_id = route_edges[index]

                    distance += max(
                        edge_lengths.get(edge_id, 0.0),
                        0.0,
                    )

                    while (
                        threshold_index < len(thresholds)
                        and distance
                        >= thresholds[threshold_index]
                    ):
                        triggers.add(edge_id)
                        threshold_index += 1

                    if threshold_index >= len(thresholds):
                        break

            element.clear()
    # Keep the rerouter valid even if no background route currently
    # traverses one of the closed edges.
    triggers.update(closure_edge_ids)

    return sorted(triggers)


def _write_native_closure_rerouter(
    *,
    run_dir: Path,
    name: str,
    network_path: Path,
    route_path: Path,
    closures: list[dict[str, Any]],
    mapping: dict[str, list[str]],
    departure_iso: str,
    warmup_seconds: int,
    end_second: int,
) -> Path:
    """Generate one native SUMO rerouter containing all hard closures."""
    edge_lengths, lane_counts = (
        _read_sumo_edge_metadata(network_path)
    )

    resolved: list[
        tuple[dict[str, Any], list[str], int, int]
    ] = []

    closure_edge_ids: set[str] = set()

    for closure in closures:
        sumo_edges = sorted(
            {
                edge_id
                for app_edge_id in closure["app_edge_ids"]
                for edge_id in mapping[str(app_edge_id)]
            }
        )

        begin, end = _native_closure_window(
            closure,
            departure_iso,
            warmup_seconds,
            end_second,
        )

        if begin >= end:
            continue

        closure_edge_ids.update(sumo_edges)

        resolved.append(
            (closure, sumo_edges, begin, end)
        )

    if not resolved:
        raise ValueError(
            "Native closure rerouter received no active "
            "full/lane restrictions"
        )

    trigger_edges = _collect_closure_trigger_edges(
        route_path,
        closure_edge_ids,
        edge_lengths,
    )

    if not trigger_edges:
        raise ValueError(
            "Could not determine any SUMO rerouter trigger edges"
        )

    root = ET.Element("additional")

    rerouter = ET.SubElement(
        root,
        "rerouter",
        {
            "id": f"{name}-native-closures",
            "edges": " ".join(trigger_edges),
            "probability": "1",
        },
    )

    timeline = {0, end_second + 1}

    for _, _, begin, end in resolved:
        timeline.add(begin)
        timeline.add(end)

    points = sorted(timeline)

    disallow = " ".join(
        sorted(str(value) for value in VEHICLE_CLASSES)
    )

    for begin, end in zip(points, points[1:]):
        active = [
            (closure, sumo_edges)
            for closure, sumo_edges, start, stop
            in resolved
            if start <= begin < stop
        ]

        if not active:
            continue

        interval = ET.SubElement(
            rerouter,
            "interval",
            {
                "begin": str(begin),
                "end": str(end),
            },
        )

        for closure, sumo_edges in active:
            kind = closure["restriction_type"]

            for edge_id in sumo_edges:
                if kind == "full":
                    ET.SubElement(
                        interval,
                        "closingReroute",
                        {
                            "id": edge_id,
                            "disallow": disallow,
                        },
                    )

                elif kind == "lane":
                    lane_count = lane_counts.get(edge_id)

                    if lane_count is None:
                        raise ValueError(
                            f"Missing lane metadata for "
                            f"SUMO edge {edge_id}"
                        )

                    remaining = int(
                        closure["remaining_lanes"]
                    )

                    for lane_index in range(
                        remaining,
                        lane_count,
                    ):
                        ET.SubElement(
                            interval,
                            "closingLaneReroute",
                            {
                                "id": (
                                    f"{edge_id}_{lane_index}"
                                ),
                                "disallow": disallow,
                            },
                        )

    output_path = (
        run_dir / f"{name}-closures.add.xml"
    )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tree.write(
        output_path,
        encoding="utf-8",
        xml_declaration=True,
    )

    return output_path

def _capture_edge_stats(vehicle_ids: list[str], totals: dict[str, dict[str, Any]]) -> None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for vehicle_id in vehicle_ids:
        try:
            edge_id = libsumo.vehicle.getRoadID(vehicle_id)
            if edge_id and not edge_id.startswith(":"):
                grouped[edge_id].append(float(libsumo.vehicle.getSpeed(vehicle_id)))
        except libsumo.TraCIException:
            continue
    for edge_id, speeds in grouped.items():
        record = totals.setdefault(
            edge_id,
            {"count_total": 0.0, "speed_total": 0.0, "samples": 0, "geometry": _edge_coordinates(edge_id)},
        )
        record["count_total"] += len(speeds)
        record["speed_total"] += sum(speeds) / len(speeds)
        record["samples"] += 1


def _capture_frame(
    vehicle_ids: list[str],
    selected_id: str,
    limit: int,
    traveled: list[list[float]],
    last_projected: list[list[float]],
    elapsed: int,
) -> tuple[dict[str, Any], list[list[float]]]:
    background = [vehicle_id for vehicle_id in vehicle_ids if vehicle_id != selected_id]
    sampled = sorted(background, key=_stable_vehicle_rank)[:limit]
    agents: list[dict[str, Any]] = []
    for vehicle_id in sampled:
        try:
            x, y = libsumo.vehicle.getPosition(vehicle_id)
            lon, lat = libsumo.simulation.convertGeo(x, y)
            speed = float(libsumo.vehicle.getSpeed(vehicle_id))
            allowed = max(float(libsumo.vehicle.getAllowedSpeed(vehicle_id)), 0.1)
            ratio = speed / allowed
            agents.append({
                "id": vehicle_id,
                "coordinate": [round(lon, 6), round(lat, 6)],
                "congestion": "heavy" if ratio < 0.35 else "slow" if ratio < 0.7 else "free",
                "opacity": 0.82,
            })
        except (libsumo.TraCIException, KeyError):
            continue
    trip_coordinate = None
    projected = last_projected
    selected_route_edges: list[str] = []
    if selected_id in set(vehicle_ids):
        try:
            x, y = libsumo.vehicle.getPosition(selected_id)
            lon, lat = libsumo.simulation.convertGeo(x, y)
            trip_coordinate = [round(lon, 6), round(lat, 6)]
            if not traveled or traveled[-1] != trip_coordinate:
                traveled.append(trip_coordinate)
            route = list(libsumo.vehicle.getRoute(selected_id))
            selected_route_edges = route
            route_index = max(0, int(libsumo.vehicle.getRouteIndex(selected_id)))
            projected = _route_coordinates(route[route_index:])
        except (libsumo.TraCIException, KeyError):
            pass
    return ({
        "elapsed_seconds": elapsed,
        "agents": agents,
        "trip_coordinate": trip_coordinate,
        "traveled_route": list(traveled),
        "projected_route": projected,
        "selected_route_edges": selected_route_edges,
    }, projected)


def _stable_vehicle_rank(vehicle_id: str) -> str:
    return hashlib.sha1(vehicle_id.encode("utf-8")).hexdigest()


def _route_hash(edge_ids: list[str]) -> str | None:
    if not edge_ids:
        return None
    return hashlib.sha256("\0".join(edge_ids).encode("utf-8")).hexdigest()[:20]


def _route_coordinates(edge_ids: list[str]) -> list[list[float]]:
    coordinates: list[list[float]] = []
    for edge_id in edge_ids:
        shape = libsumo.lane.getShape(f"{edge_id}_0")
        for x, y in shape:
            lon, lat = libsumo.simulation.convertGeo(x, y)
            point = [round(lon, 6), round(lat, 6)]
            if not coordinates or coordinates[-1] != point:
                coordinates.append(point)
    return coordinates


def _edge_coordinates(edge_id: str) -> list[list[float]]:
    try:
        return _route_coordinates([edge_id])
    except libsumo.TraCIException:
        return []


def _edge_changes(
    edge_names: dict[str, str],
    baseline: dict[str, dict[str, Any]],
    scenario: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for edge_id in set(baseline) | set(scenario):
        before = baseline.get(edge_id, {"count_total": 0, "speed_total": 0, "samples": 0, "geometry": []})
        after = scenario.get(edge_id, {"count_total": 0, "speed_total": 0, "samples": 0, "geometry": []})
        before_count = before["count_total"] / before["samples"] if before["samples"] else 0.0
        after_count = after["count_total"] / after["samples"] if after["samples"] else 0.0
        delta = after_count - before_count
        if abs(delta) < 0.5:
            continue
        geometry = after["geometry"] or before["geometry"]
        road_name = edge_names.get(edge_id, edge_id)
        changes.append({
            "sumo_edge_id": edge_id,
            "road_name": road_name,
            "baseline_mean_active_vehicles": round(before_count, 3),
            "scenario_mean_active_vehicles": round(after_count, 3),
            "change_mean_active_vehicles": round(delta, 3),
            "baseline_mean_speed_mps": round(before["speed_total"] / before["samples"], 3) if before["samples"] else None,
            "scenario_mean_speed_mps": round(after["speed_total"] / after["samples"], 3) if after["samples"] else None,
            "geometry": {"type": "LineString", "coordinates": geometry},
        })
    return sorted(changes, key=lambda item: abs(item["change_mean_active_vehicles"]), reverse=True)


def _trip_delta(baseline: dict[str, Any] | None, scenario: dict[str, Any] | None) -> float | None:
    if not baseline or not scenario:
        return None
    return round(float(scenario["duration"]) - float(baseline["duration"]), 3)



def _variant_cli() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one isolated regional SUMO variant."
        )
    )
    parser.add_argument(
        "--variant-request",
        type=Path,
        required=True,
    )

    args = parser.parse_args()

    return _run_variant_child(
        args.variant_request.resolve()
    )


if __name__ == "__main__":
    raise SystemExit(_variant_cli())
