"""Recover station telemetry from a complete immutable checkpoint chain."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from backend.app.schemas.sumo_station_cross_section_policy import (
    StationCrossSectionPolicyManifestV1,
)
from backend.app.schemas.sumo_station_telemetry import (
    STATION_OBSERVER_STATE_VERSION,
    StationTelemetryManifestV1,
    StationTelemetryRecoveryManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.departure_provenance_service import (
    DEPARTURE_FRAGMENT,
    load_native_departure_fragment,
    reconcile_departure_records,
)
from backend.app.services.sumo.edge_telemetry_service import validate_native_fragment
from backend.app.services.sumo.station_telemetry_service import (
    STATION_FRAGMENT,
    STATION_PLAN,
    STATION_TELEMETRY_DIRECTORY,
    load_station_observation_plan,
    materialize_station_telemetry,
)

RECOVERY_PRODUCER_VERSION = "phase-2.2g-recovery-v1"
RECOVERY_VALIDATION_ALGORITHM = "promoted-checkpoint-chain-v1"
EXPECTED_VARIANTS = ("baseline", "scenario")
EXPECTED_CHECKPOINT_SECONDS = tuple(range(100, 901, 100))
REQUIRED_CHECKPOINT_FILES = (
    "checkpoint.json",
    "chunk-result.json.gz",
    DEPARTURE_FRAGMENT,
    "departure-vehroute.xml.gz",
    "native-edge-data.add.xml",
    "native-edge-data.xml.gz",
    "python-state.json.gz",
    STATION_FRAGMENT,
    "sumo-state.xml.gz",
    "tripinfo.xml",
)


class StationTelemetryRecoveryError(RuntimeError):
    """Promoted checkpoints cannot safely produce station telemetry."""


def recover_station_telemetry_from_checkpoints(
    *,
    run_directory: Path,
    expected_run_identity: str,
) -> tuple[StationTelemetryManifestV1, StationTelemetryRecoveryManifestV1]:
    """Validate 18 promoted checkpoints and reuse the production finalizer."""
    run_directory = run_directory.resolve()
    final_directory = run_directory / STATION_TELEMETRY_DIRECTORY
    if final_directory.exists():
        raise StationTelemetryRecoveryError(
            "station telemetry output already exists; recovery will not overwrite it"
        )
    request_path = run_directory / "request.json"
    result_path = run_directory / "result.json"
    request = _read_json(request_path)
    result = _read_json(result_path)
    if request.get("run_identity") != expected_run_identity:
        raise StationTelemetryRecoveryError("run identity does not match recovery gate")
    if request.get("application_run_id") != run_directory.name:
        raise StationTelemetryRecoveryError(
            "application run identity does not match the run directory"
        )
    if result.get("status") != "failed" or "free-flow floor" not in str(
        result.get("error", "")
    ):
        raise StationTelemetryRecoveryError(
            "recovery requires the preserved post-checkpoint free-flow failure"
        )
    payload = request.get("request")
    if not isinstance(payload, dict):
        raise StationTelemetryRecoveryError("run request payload is missing")
    if payload.get("station_telemetry_interval_seconds") != 900:
        raise StationTelemetryRecoveryError("run did not request 900-second stations")
    if int(payload.get("seed", -1)) != int(request.get("seed", -2)):
        raise StationTelemetryRecoveryError("run seed provenance is inconsistent")
    if (
        int(payload.get("analysis_minutes", -1))
        + int(payload.get("warmup_minutes", -1))
        != 15
    ):
        raise StationTelemetryRecoveryError(
            "checkpoint recovery currently requires exactly one 900-second interval"
        )

    plan_path = run_directory / STATION_PLAN
    plan = load_station_observation_plan(plan_path)
    if plan["policy_content_digest"] != payload.get(
        "station_cross_section_policy_digest"
    ):
        raise StationTelemetryRecoveryError("station policy identity is inconsistent")
    policy_directory = Path(request["station_cross_section_policy_path"]).resolve()
    policy_manifest = StationCrossSectionPolicyManifestV1.model_validate_json(
        (policy_directory / "station-cross-section-policy-manifest.json").read_bytes()
    )
    if policy_manifest.content_digest != plan["policy_content_digest"]:
        raise StationTelemetryRecoveryError("station policy manifest digest changed")

    network_path = Path(request["network_path"]).resolve()
    network_manifest_path = Path(request["network_manifest_path"]).resolve()
    graph_manifest_path = Path(request["graph_manifest_path"]).resolve()
    network = _read_json(network_manifest_path)
    graph = _read_json(graph_manifest_path)
    if _sha256(network_path) != network["artifact"]["sha256"]:
        raise StationTelemetryRecoveryError("SUMO network hash does not match manifest")
    if policy_manifest.sumo_network_version != network["network_version"]:
        raise StationTelemetryRecoveryError("policy and SUMO network versions differ")
    if policy_manifest.graph_version != graph["graph_version"]:
        raise StationTelemetryRecoveryError("policy and graph versions differ")

    request_digest_before = _sha256(request_path)
    result_digest_before = _sha256(result_path)
    checkpoint_records: list[dict[str, Any]] = []
    departure_fragments_by_variant: dict[
        str, list[tuple[str, list[dict[str, Any]]]]
    ] = {}
    route_path: Path | None = None
    demand_manifest: dict[str, Any] | None = None
    final_results: dict[str, dict[str, Any]] = {}
    for variant in EXPECTED_VARIANTS:
        records, variant_route, variant_demand, final_result, fragments = (
            _validate_variant_checkpoint_chain(
                run_directory=run_directory,
                variant=variant,
                request=request,
                payload=payload,
                plan=plan,
                network_path=network_path,
            )
        )
        checkpoint_records.extend(records)
        departure_fragments_by_variant[variant] = fragments
        final_results[variant] = final_result
        if route_path is None:
            route_path = variant_route
            demand_manifest = variant_demand
        elif route_path != variant_route or canonical_json(
            demand_manifest
        ) != canonical_json(variant_demand):
            raise StationTelemetryRecoveryError(
                "baseline and scenario demand provenance differ"
            )
    # Vehicle IDs are unique only within one simulated world. Baseline and
    # scenario deliberately reuse demand vehicle IDs, so reconciling both
    # variants together would turn valid world-local provenance into a false
    # conflict (including their necessarily different source checkpoint IDs).
    for variant in EXPECTED_VARIANTS:
        reconcile_departure_records(departure_fragments_by_variant[variant])
    assert demand_manifest is not None
    if demand_manifest["network"]["network_version"] != network["network_version"]:
        raise StationTelemetryRecoveryError("demand and SUMO network versions differ")
    if _sha256(route_path) != demand_manifest["artifacts"]["routes"]["sha256"]:
        raise StationTelemetryRecoveryError("demand route hash does not match manifest")

    selected_trip_diagnostic = _selected_trip_diagnostic(final_results)
    recovery_payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "commute_help_station_telemetry_checkpoint_recovery",
        "artifact_status": "complete",
        "generated_at": datetime.now(UTC),
        "producer_name": "station_telemetry_checkpoint_recovery",
        "producer_version": RECOVERY_PRODUCER_VERSION,
        "validation_algorithm": RECOVERY_VALIDATION_ALGORITHM,
        "application_run_id": str(request["application_run_id"]),
        "run_identity": expected_run_identity,
        "source_run_wrapper_status": "failed",
        "source_run_wrapper_error": str(result["error"]),
        "recovery_reason": "post_checkpoint_selected_trip_diagnostic_failure",
        "checkpoint_count": len(checkpoint_records),
        "final_checkpoint_second": 900,
        "network_version": str(network["network_version"]),
        "graph_version": str(graph["graph_version"]),
        "seed": int(payload["seed"]),
        "demand_version": str(demand_manifest["demand_version"]),
        "demand_routes_sha256": str(demand_manifest["artifacts"]["routes"]["sha256"]),
        "policy_content_digest": str(plan["policy_content_digest"]),
        "observation_plan_digest": str(plan["observation_plan_digest"]),
        "checkpoints": checkpoint_records,
        "selected_trip_diagnostic": selected_trip_diagnostic,
        "station_telemetry_content_digest": None,
        "station_telemetry_output_sha256": None,
        "station_telemetry_row_count": None,
    }
    recovery_payload["content_digest"] = _content_digest(recovery_payload)
    StationTelemetryRecoveryManifestV1.model_validate(recovery_payload)
    try:
        telemetry = materialize_station_telemetry(
            run_dir=run_directory,
            application_run_id=str(request["application_run_id"]),
            run_identity=expected_run_identity,
            network_manifest_path=network_manifest_path,
            graph_manifest_path=graph_manifest_path,
            demand_manifest=demand_manifest,
            plan=plan,
            seed=int(payload["seed"]),
            simulation_end_seconds=900,
            recovery_provenance=recovery_payload,
        )
    except BaseException:
        if final_directory.exists():
            # This directory was created by this recovery attempt, never source evidence.
            shutil.rmtree(final_directory)
        pending_directory = run_directory / f".{STATION_TELEMETRY_DIRECTORY}.pending"
        if pending_directory.exists():
            shutil.rmtree(pending_directory)
        raise
    if (
        _sha256(request_path) != request_digest_before
        or _sha256(result_path) != result_digest_before
    ):
        raise StationTelemetryRecoveryError(
            "recovery unexpectedly changed source request/result truth"
        )
    recovery = StationTelemetryRecoveryManifestV1.model_validate_json(
        (final_directory / "station-telemetry-recovery.json").read_bytes()
    )
    return telemetry, recovery


def _validate_variant_checkpoint_chain(
    *,
    run_directory: Path,
    variant: str,
    request: dict[str, Any],
    payload: dict[str, Any],
    plan: dict[str, Any],
    network_path: Path,
) -> tuple[
    list[dict[str, Any]],
    Path,
    dict[str, Any],
    dict[str, Any],
    list[tuple[str, list[dict[str, Any]]]],
]:
    root = run_directory / "checkpoints" / variant
    actual = {
        int(path.name): path
        for path in root.iterdir()
        if path.is_dir() and path.name.isdigit()
    }
    if set(actual) != set(EXPECTED_CHECKPOINT_SECONDS):
        raise StationTelemetryRecoveryError(
            f"{variant} must contain exactly nine promoted checkpoints through 900"
        )
    records: list[dict[str, Any]] = []
    fragments: list[tuple[str, list[dict[str, Any]]]] = []
    route_path: Path | None = None
    demand_manifest: dict[str, Any] | None = None
    final_result: dict[str, Any] | None = None
    previous_end = 0
    for chunk_index, end_second in enumerate(EXPECTED_CHECKPOINT_SECONDS, start=1):
        checkpoint = actual[end_second]
        missing = [
            name
            for name in REQUIRED_CHECKPOINT_FILES
            if not (checkpoint / name).is_file()
        ]
        if missing:
            raise StationTelemetryRecoveryError(
                f"{variant} checkpoint {end_second} is missing {missing}"
            )
        metadata = _read_json(checkpoint / "checkpoint.json")
        expected_metadata = {
            "completed": True,
            "variant": variant,
            "chunk_index": chunk_index,
            "total_chunks": 9,
            "chunk_start_second": previous_end,
            "chunk_end_second": end_second,
            "compute_chunk_seconds": 100,
            "telemetry_interval_seconds": 900,
            "station_telemetry_interval_seconds": 900,
            "station_observation_plan_digest": plan["observation_plan_digest"],
        }
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise StationTelemetryRecoveryError(
                f"{variant} checkpoint {end_second} metadata is inconsistent"
            )
        child_request = _read_json(
            run_directory / f"{variant}-chunk-{chunk_index:05d}-request.json"
        )
        _validate_child_request(
            child_request=child_request,
            run_directory=run_directory,
            checkpoint=checkpoint,
            variant=variant,
            chunk_index=chunk_index,
            start_second=previous_end,
            end_second=end_second,
            payload=payload,
            network_path=network_path,
        )
        child_route = Path(child_request["route_path"]).resolve()
        child_demand = _read_json(child_route.parent / "demand-manifest.json")
        if route_path is None:
            route_path = child_route
            demand_manifest = child_demand
        elif child_route != route_path or canonical_json(
            child_demand
        ) != canonical_json(demand_manifest):
            raise StationTelemetryRecoveryError(
                f"{variant} demand provenance changed across checkpoints"
            )

        departure = load_native_departure_fragment(checkpoint / DEPARTURE_FRAGMENT)
        if (
            departure["checkpoint_id"] != f"{variant}:{end_second}"
            or (metadata.get("departure_provenance_fragment") or {}).get(
                "content_digest"
            )
            != departure["content_digest"]
        ):
            raise StationTelemetryRecoveryError(
                f"{variant} checkpoint {end_second} departure fragment mismatch"
            )
        fragments.append((f"{variant}:{end_second}", departure["records"]))
        station_fragment = _read_json(checkpoint / STATION_FRAGMENT)
        if station_fragment.get("schema_version") != 1 or not isinstance(
            station_fragment.get("rows"), list
        ):
            raise StationTelemetryRecoveryError("station fragment is invalid")
        station_rows = station_fragment["rows"]
        if end_second < 900 and station_rows:
            raise StationTelemetryRecoveryError(
                "partial station interval was promoted before 900 seconds"
            )
        if end_second == 900 and (
            len(station_rows) != 356
            or len(
                {
                    (
                        row.get("variant"),
                        row.get("station_id"),
                        row.get("interval_start_seconds"),
                    )
                    for row in station_rows
                }
            )
            != 356
        ):
            raise StationTelemetryRecoveryError(
                f"{variant} final station fragment is not dense and unique"
            )
        if any(row.get("variant") != variant for row in station_rows):
            raise StationTelemetryRecoveryError("station fragment variant mismatch")

        python_state = _read_gzip_json(checkpoint / "python-state.json.gz")
        observer_state = python_state.get("station_observer_state") or {}
        if (
            python_state.get("schema_version") != 1
            or python_state.get("variant") != variant
            or python_state.get("sim_second") != end_second
            or observer_state.get("schema_version") != STATION_OBSERVER_STATE_VERSION
            or observer_state.get("observation_plan_digest")
            != plan["observation_plan_digest"]
        ):
            raise StationTelemetryRecoveryError(
                f"{variant} checkpoint {end_second} Python state is incompatible"
            )
        chunk_result = _read_gzip_json(checkpoint / "chunk-result.json.gz")
        expected_status = "completed" if end_second == 900 else "checkpointed"
        if chunk_result.get("status") != expected_status:
            raise StationTelemetryRecoveryError(
                f"{variant} checkpoint {end_second} result status is invalid"
            )
        if end_second == 900:
            final_result = chunk_result
        validate_native_fragment(
            checkpoint / "native-edge-data.xml.gz",
            expected_begin_seconds=previous_end,
            expected_end_seconds=end_second,
        )
        _validate_gzip(checkpoint / "sumo-state.xml.gz")
        _validate_xml(checkpoint / "departure-vehroute.xml.gz")
        _validate_xml(checkpoint / "tripinfo.xml")
        file_identities = {
            name: {
                "sha256": _sha256(checkpoint / name),
                "byte_count": (checkpoint / name).stat().st_size,
            }
            for name in REQUIRED_CHECKPOINT_FILES
        }
        records.append(
            {
                "variant": variant,
                "chunk_index": chunk_index,
                "chunk_start_second": previous_end,
                "chunk_end_second": end_second,
                "station_fragment_row_count": len(station_rows),
                "departure_provenance_content_digest": departure["content_digest"],
                "files": file_identities,
            }
        )
        previous_end = end_second
    assert (
        route_path is not None
        and demand_manifest is not None
        and final_result is not None
    )
    summary = _read_gzip_json(run_directory / f"{variant}-variant-result.json.gz")
    if canonical_json(summary) != canonical_json(final_result):
        raise StationTelemetryRecoveryError(
            f"{variant} summary result differs from final checkpoint result"
        )
    return records, route_path, demand_manifest, final_result, fragments


def _validate_child_request(
    *,
    child_request: dict[str, Any],
    run_directory: Path,
    checkpoint: Path,
    variant: str,
    chunk_index: int,
    start_second: int,
    end_second: int,
    payload: dict[str, Any],
    network_path: Path,
) -> None:
    if (
        child_request.get("name") != variant
        or child_request.get("chunk_index") != chunk_index
        or child_request.get("total_chunks") != 9
        or child_request.get("chunk_start_second") != start_second
        or child_request.get("chunk_end_second") != end_second
        or child_request.get("compute_chunk_seconds") != 100
        or child_request.get("final_chunk") != (end_second == 900)
        or canonical_json(child_request.get("payload")) != canonical_json(payload)
        or Path(child_request["run_dir"]).resolve() != run_directory
        or Path(child_request["network_path"]).resolve() != network_path
    ):
        raise StationTelemetryRecoveryError(
            f"{variant} child request {chunk_index} identity is inconsistent"
        )
    claimed_checkpoint = Path(child_request["checkpoint_dir"]).resolve()
    if (
        claimed_checkpoint.parent != checkpoint.parent
        or claimed_checkpoint.name != f"{checkpoint.name}.tmp"
    ):
        raise StationTelemetryRecoveryError("child checkpoint target is inconsistent")
    previous = checkpoint.parent / f"{start_second:08d}"
    expected_sumo = previous / "sumo-state.xml.gz" if start_second else None
    expected_python = previous / "python-state.json.gz" if start_second else None
    claimed_sumo = (
        Path(child_request["load_state_path"]).resolve()
        if child_request.get("load_state_path")
        else None
    )
    claimed_python = (
        Path(child_request["python_state_path"]).resolve()
        if child_request.get("python_state_path")
        else None
    )
    if claimed_sumo != expected_sumo or claimed_python != expected_python:
        raise StationTelemetryRecoveryError("checkpoint parent chain is inconsistent")


def _selected_trip_diagnostic(
    final_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "classification": "truncated_selected_trip_not_arrived",
        "free_flow_validation_preserved": True,
    }
    for variant, result in sorted(final_results.items()):
        trip = result.get("selected_trip") or {}
        floor = float(result["selected_free_flow_seconds"])
        minimum = max(0.1, floor - max(5.0, floor * 0.03))
        duration = float(trip.get("duration", 0.0))
        values[variant] = {
            "selected_trip_duration_seconds": duration,
            "selected_trip_arrival_seconds": float(trip.get("arrival", -1.0)),
            "sumo_free_flow_seconds": floor,
            "minimum_allowed_seconds": round(minimum, 3),
            "difference_from_minimum_seconds": round(duration - minimum, 3),
            "selected_initial_route_hash": result.get("selected_initial_route_hash"),
            "selected_final_route_hash": result.get("selected_final_route_hash"),
            "selected_trip_rerouted": bool(result.get("selected_trip_rerouted")),
            "valid": duration >= minimum,
        }
    return values


def _content_digest(payload: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_digest"}
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise StationTelemetryRecoveryError(f"expected JSON object: {path}")
    return value


def _read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise StationTelemetryRecoveryError(f"expected gzip JSON object: {path}")
    return value


def _validate_gzip(path: Path) -> None:
    with gzip.open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            pass


def _validate_xml(path: Path) -> None:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as source:
            ET.parse(source)
    else:
        ET.parse(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
