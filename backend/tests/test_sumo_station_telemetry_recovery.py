"""Phase 2.2g checkpoint-only station telemetry recovery tests."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.departure_provenance_service import (
    DEPARTURE_FRAGMENT_VERSION,
    DepartureProvenanceError,
    write_native_departure_fragment,
)
from backend.app.services.sumo.station_telemetry_recovery_service import (
    StationTelemetryRecoveryError,
    recover_station_telemetry_from_checkpoints,
)
from backend.app.services.sumo.station_telemetry_service import (
    materialize_station_telemetry,
    write_station_fragment,
    write_station_observation_plan,
)

RUN_IDENTITY = "a" * 64
POLICY_DIGEST = "b" * 64


def test_complete_failed_run_recovers_without_erasing_failure(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "recovery")
    original_result = (fixture["run"] / "result.json").read_bytes()
    telemetry, recovery = recover_station_telemetry_from_checkpoints(
        run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
    )
    assert telemetry.output.row_count == 712
    assert recovery.source_run_wrapper_status == "failed"
    assert recovery.checkpoint_count == 18
    assert (fixture["run"] / "result.json").read_bytes() == original_result
    rows = pq.read_table(
        fixture["run"] / "station-telemetry-15m" / "station-telemetry-15m.parquet"
    ).to_pandas()
    assert len(rows) == 712
    assert set(rows.groupby("variant").size()) == {356}
    assert not rows.duplicated(
        ["variant", "station_id", "interval_start_seconds"]
    ).any()
    assert rows["crossing_step_speed_sample_count"].eq(0).all()
    assert rows["crossing_step_speed_mean_mps"].isna().all()


@pytest.mark.parametrize("missing_second", [500, 900])
def test_incomplete_checkpoint_set_is_rejected(
    tmp_path: Path, missing_second: int
) -> None:
    fixture = _recovery_fixture(tmp_path / f"missing-{missing_second}")
    shutil.rmtree(fixture["run"] / "checkpoints" / "baseline" / f"{missing_second:08d}")
    with pytest.raises(StationTelemetryRecoveryError, match="exactly nine"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
        )


def test_departure_fragment_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "bad-fragment")
    path = (
        fixture["run"]
        / "checkpoints"
        / "baseline"
        / "00000100"
        / "departure-provenance-fragment.json"
    )
    value = json.loads(path.read_text())
    value["content_digest"] = "0" * 64
    path.write_text(canonical_json(value) + "\n")
    with pytest.raises(Exception, match="digest"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
        )


def test_run_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "identity")
    with pytest.raises(StationTelemetryRecoveryError, match="run identity"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity="c" * 64
        )


@pytest.mark.parametrize("scenario_position", [2157.32, 2199.75])
def test_same_vehicle_id_is_reconciled_independently_per_variant(
    tmp_path: Path, scenario_position: float
) -> None:
    fixture = _recovery_fixture(tmp_path / f"variant-scope-{scenario_position}")
    _set_departure_record(
        fixture["run"], variant="baseline", end_second=100, position_m=2157.32
    )
    _set_departure_record(
        fixture["run"],
        variant="scenario",
        end_second=100,
        position_m=scenario_position,
    )

    telemetry, _ = recover_station_telemetry_from_checkpoints(
        run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
    )

    assert telemetry.output.row_count == 712
    rows = pq.read_table(
        fixture["run"] / "station-telemetry-15m" / "station-telemetry-15m.parquet"
    ).to_pandas()
    assert rows.groupby("variant").size().to_dict() == {
        "baseline": 356,
        "scenario": 356,
    }


@pytest.mark.parametrize("variant", ["baseline", "scenario"])
def test_conflicting_numeric_provenance_within_variant_hard_fails(
    tmp_path: Path, variant: str
) -> None:
    fixture = _recovery_fixture(tmp_path / f"within-{variant}-conflict")
    _set_departure_record(
        fixture["run"], variant=variant, end_second=100, position_m=100.0
    )
    _set_departure_record(
        fixture["run"], variant=variant, end_second=200, position_m=125.0
    )

    with pytest.raises(DepartureProvenanceError, match="Conflicting native"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
        )
    assert not (fixture["run"] / "station-telemetry-15m").exists()


def test_duplicate_station_identity_is_rejected(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "duplicate")
    path = (
        fixture["run"]
        / "checkpoints"
        / "scenario"
        / "00000900"
        / "station-telemetry-fragment.json"
    )
    value = json.loads(path.read_text())
    value["rows"][-1] = dict(value["rows"][0])
    path.write_text(canonical_json(value) + "\n")
    with pytest.raises(StationTelemetryRecoveryError, match="dense and unique"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
        )


def test_incomplete_departure_provenance_remains_station_local(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "incomplete", incomplete_station=True)
    recover_station_telemetry_from_checkpoints(
        run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
    )
    rows = pq.read_table(
        fixture["run"] / "station-telemetry-15m" / "station-telemetry-15m.parquet"
    ).to_pandas()
    incomplete = rows.loc[
        rows["variant"].eq("baseline") & rows["station_id"].eq("station-000")
    ].iloc[0]
    assert incomplete.unresolved_direct_departure_count == 1
    assert not incomplete.flow_measurement_complete
    assert rows.loc[
        rows["station_id"].ne("station-000"), "flow_measurement_complete"
    ].all()


def test_completeness_flag_must_match_unresolved_count(tmp_path: Path) -> None:
    fixture = _recovery_fixture(tmp_path / "bad-completeness")
    path = (
        fixture["run"]
        / "checkpoints"
        / "baseline"
        / "00000900"
        / "station-telemetry-fragment.json"
    )
    value = json.loads(path.read_text())
    value["rows"][0]["unresolved_direct_departure_count"] = 1
    value["rows"][0]["flow_measurement_complete"] = True
    path.write_text(canonical_json(value) + "\n")
    with pytest.raises(Exception, match="completeness"):
        recover_station_telemetry_from_checkpoints(
            run_directory=fixture["run"], expected_run_identity=RUN_IDENTITY
        )
    assert not (fixture["run"] / "station-telemetry-15m").exists()


def test_recovery_and_normal_finalization_have_identical_core_telemetry(
    tmp_path: Path,
) -> None:
    recovered = _recovery_fixture(tmp_path / "recovered")
    normal = _recovery_fixture(tmp_path / "normal")
    recovered_manifest, recovered_lineage = recover_station_telemetry_from_checkpoints(
        run_directory=recovered["run"], expected_run_identity=RUN_IDENTITY
    )
    normal_manifest = materialize_station_telemetry(
        run_dir=normal["run"],
        application_run_id=normal["run"].name,
        run_identity=RUN_IDENTITY,
        network_manifest_path=normal["network_manifest"],
        graph_manifest_path=normal["graph_manifest"],
        demand_manifest=normal["demand_manifest"],
        plan=normal["plan"],
        seed=842901,
        simulation_end_seconds=900,
    )
    assert recovered_manifest.row_content_sha256 == normal_manifest.row_content_sha256
    assert recovered_manifest.output.sha256 == normal_manifest.output.sha256
    assert recovered_manifest.content_digest == normal_manifest.content_digest
    assert (
        recovered_lineage.station_telemetry_content_digest
        == normal_manifest.content_digest
    )


def test_recovery_identity_is_deterministic_for_same_checkpoint_evidence(
    tmp_path: Path,
) -> None:
    first = _recovery_fixture(tmp_path / "same")
    _, one = recover_station_telemetry_from_checkpoints(
        run_directory=first["run"], expected_run_identity=RUN_IDENTITY
    )
    shutil.rmtree(first["run"] / "station-telemetry-15m")
    _, two = recover_station_telemetry_from_checkpoints(
        run_directory=first["run"], expected_run_identity=RUN_IDENTITY
    )
    assert one.content_digest == two.content_digest
    assert one.station_telemetry_content_digest == two.station_telemetry_content_digest


def _recovery_fixture(
    run: Path, *, incomplete_station: bool = False
) -> dict[str, object]:
    run.mkdir(parents=True)
    network_path = run / "network.xml"
    network_path.write_text("<net/>\n")
    network_sha = _sha256(network_path)
    network_manifest = run / "network-manifest.json"
    network_manifest.write_text(
        canonical_json(
            {
                "network_version": "network-v1",
                "sumo_version": "1.27.1",
                "artifact": {"filename": "network.xml", "sha256": network_sha},
            }
        )
        + "\n"
    )
    graph_manifest = run / "graph-manifest.json"
    graph_manifest.write_text(canonical_json({"graph_version": "graph-v1"}) + "\n")
    policy = run / "policy"
    policy.mkdir()
    empty_output = {
        "relative_path": "unused",
        "sha256": "d" * 64,
        "byte_count": 0,
        "row_count": 0,
    }
    (policy / "station-cross-section-policy-manifest.json").write_text(
        canonical_json(
            {
                "schema_version": 1,
                "artifact_type": "commute_help_station_cross_section_acceptance_policy",
                "artifact_status": "complete_policy",
                "calibration_status": "not_calibrated",
                "policy_name": "historical_station_sumo_cross_section_policy",
                "policy_version": "phase-2.2d-policy-v1",
                "policy_algorithm_version": "distance-boundary-direction-v1",
                "source_station_cross_section_digest": "1" * 64,
                "source_station_cross_section_sha256": "2" * 64,
                "source_edge_map_sha256": "3" * 64,
                "source_relation_digest": "4" * 64,
                "graph_version": "graph-v1",
                "sumo_network_version": "network-v1",
                "generated_at": "2026-01-01T00:00:00Z",
                "decision_rules": {},
                "policy_output": empty_output,
                "review_output": empty_output,
                "summary_output": empty_output,
                "report_output": empty_output,
                "row_content_sha256": "5" * 64,
                "content_digest": POLICY_DIGEST,
            }
        )
        + "\n"
    )
    stations = [
        {
            "station_id": f"station-{index:03d}",
            "app_edge_id": f"app-{index:03d}",
            "cross_section_type": "within_edge_position",
            "sumo_edge_id": f"edge-{index:03d}",
            "position_m": 50.0,
            "transition_from_edge_id": None,
            "transition_to_edge_id": None,
            "direction": "NORTH",
            "relation_id": f"relation-{index:03d}",
            "policy_row_id": f"policy-{index:03d}",
        }
        for index in range(356)
    ]
    plan = {
        "plan_version": "phase-2.2e-plan-v1",
        "policy_name": "historical_station_sumo_cross_section_policy",
        "policy_version": "phase-2.2d-policy-v1",
        "policy_content_digest": POLICY_DIGEST,
        "observer_algorithm": "ordered-position-transition-v1",
        "observer_implementation": "subscription-filtered-v1",
        "stations": stations,
        "relevant_edge_ids": [row["sumo_edge_id"] for row in stations],
        "station_edge_predecessors": [],
    }
    plan["observation_plan_digest"] = hashlib.sha256(
        canonical_json(plan).encode()
    ).hexdigest()
    write_station_observation_plan(run / "station-observation-plan.json", plan)
    route_dir = run / "demand"
    route_dir.mkdir()
    route_path = route_dir / "regional.rou.xml.gz"
    _gzip_bytes(route_path, b"<routes/>\n")
    demand_manifest = {
        "demand_version": "demand-v1",
        "network": {"network_version": "network-v1"},
        "artifacts": {"routes": {"sha256": _sha256(route_path)}},
    }
    (route_dir / "demand-manifest.json").write_text(
        canonical_json(demand_manifest) + "\n"
    )
    payload = {
        "analysis_minutes": 15,
        "warmup_minutes": 0,
        "seed": 842901,
        "edge_telemetry_interval_seconds": 900,
        "station_telemetry_interval_seconds": 900,
        "station_cross_section_policy_digest": POLICY_DIGEST,
    }
    request = {
        "application_run_id": run.name,
        "run_identity": RUN_IDENTITY,
        "seed": 842901,
        "request": payload,
        "network_path": str(network_path.resolve()),
        "network_manifest_path": str(network_manifest.resolve()),
        "graph_manifest_path": str(graph_manifest.resolve()),
        "station_cross_section_policy_path": str(policy.resolve()),
    }
    (run / "request.json").write_text(canonical_json(request) + "\n")
    failure = "baseline selected trip violated the SUMO-native physical free-flow floor"
    (run / "result.json").write_text(
        canonical_json({"status": "failed", "error": failure}) + "\n"
    )
    for variant in ("baseline", "scenario"):
        final_result = None
        for index, end in enumerate(range(100, 901, 100), start=1):
            start = end - 100
            checkpoint = run / "checkpoints" / variant / f"{end:08d}"
            checkpoint.mkdir(parents=True)
            rows = _station_rows(variant, incomplete_station) if end == 900 else []
            write_station_fragment(checkpoint / "station-telemetry-fragment.json", rows)
            departure = {
                "schema_version": 1,
                "provenance_version": "native-departure-child-v1",
                "checkpoint_id": f"{variant}:{end}",
                "departed_vehicle_ids": [],
                "records": [],
                "missing_numeric_vehicle_ids": [],
            }
            departure["content_digest"] = hashlib.sha256(
                canonical_json(departure).encode()
            ).hexdigest()
            write_native_departure_fragment(
                checkpoint / "departure-provenance-fragment.json", departure
            )
            observer_state = {
                "schema_version": 2,
                "observation_plan_digest": plan["observation_plan_digest"],
            }
            python_state = {
                "schema_version": 1,
                "variant": variant,
                "sim_second": end,
                "station_observer_state": observer_state,
            }
            _gzip_json(checkpoint / "python-state.json.gz", python_state)
            result = (
                {
                    "status": "completed",
                    "selected_trip": {
                        "arrival": -1.0,
                        "duration": 900.0,
                    },
                    "selected_free_flow_seconds": 1080.272,
                    "selected_initial_route_hash": "initial",
                    "selected_final_route_hash": "final",
                    "selected_trip_rerouted": True,
                }
                if end == 900
                else {"status": "checkpointed", "sim_second": end}
            )
            _gzip_json(checkpoint / "chunk-result.json.gz", result)
            final_result = result if end == 900 else final_result
            _gzip_bytes(checkpoint / "sumo-state.xml.gz", b"<state/>\n")
            _gzip_bytes(checkpoint / "departure-vehroute.xml.gz", b"<routes/>\n")
            (checkpoint / "tripinfo.xml").write_text("<tripinfos/>\n")
            (checkpoint / "native-edge-data.add.xml").write_text("<additional/>\n")
            _gzip_bytes(
                checkpoint / "native-edge-data.xml.gz",
                f'<meandata><interval begin="{start}" end="{end}"/></meandata>\n'.encode(),
            )
            metadata = {
                "schema_version": 1,
                "completed": True,
                "variant": variant,
                "chunk_index": index,
                "total_chunks": 9,
                "chunk_start_second": start,
                "chunk_end_second": end,
                "compute_chunk_seconds": 100,
                "telemetry_interval_seconds": 900,
                "telemetry_native_edge_count": 0,
                "station_telemetry_interval_seconds": 900,
                "station_observation_plan_digest": plan["observation_plan_digest"],
                "departure_provenance_fragment": {
                    "content_digest": departure["content_digest"]
                },
            }
            (checkpoint / "checkpoint.json").write_text(canonical_json(metadata) + "\n")
            previous = run / "checkpoints" / variant / f"{start:08d}"
            child_request = {
                "name": variant,
                "chunk_index": index,
                "total_chunks": 9,
                "chunk_start_second": start,
                "chunk_end_second": end,
                "compute_chunk_seconds": 100,
                "final_chunk": end == 900,
                "payload": payload,
                "run_dir": str(run.resolve()),
                "network_path": str(network_path.resolve()),
                "route_path": str(route_path.resolve()),
                "checkpoint_dir": str(
                    (checkpoint.parent / f"{checkpoint.name}.tmp").resolve()
                ),
                "load_state_path": str((previous / "sumo-state.xml.gz").resolve())
                if start
                else None,
                "python_state_path": str((previous / "python-state.json.gz").resolve())
                if start
                else None,
            }
            (run / f"{variant}-chunk-{index:05d}-request.json").write_text(
                canonical_json(child_request) + "\n"
            )
        assert final_result is not None
        _gzip_json(run / f"{variant}-variant-result.json.gz", final_result)
    return {
        "run": run,
        "network_manifest": network_manifest,
        "graph_manifest": graph_manifest,
        "demand_manifest": demand_manifest,
        "plan": plan,
    }


def _station_rows(variant: str, incomplete_station: bool) -> list[dict[str, object]]:
    rows = []
    for index in range(356):
        unresolved = (
            1 if incomplete_station and variant == "baseline" and index == 0 else 0
        )
        rows.append(
            {
                "variant": variant,
                "station_id": f"station-{index:03d}",
                "app_edge_id": f"app-{index:03d}",
                "cross_section_type": "within_edge_position",
                "sumo_edge_id": f"edge-{index:03d}",
                "transition_from_sumo_edge_id": None,
                "transition_to_sumo_edge_id": None,
                "interval_start_seconds": 0,
                "interval_end_seconds": 900,
                "crossing_count": index % 4,
                "flow_vph": float((index % 4) * 4),
                "contributing_vehicle_count": index % 4,
                "resolved_direct_departure_count": 0,
                "unresolved_direct_departure_count": unresolved,
                "flow_measurement_complete": unresolved == 0,
            }
        )
    return rows


def _set_departure_record(
    run: Path | object, *, variant: str, end_second: int, position_m: float
) -> None:
    assert isinstance(run, Path)
    checkpoint = run / "checkpoints" / variant / f"{end_second:08d}"
    checkpoint_id = f"{variant}:{end_second}"
    vehicle_id = "od-005992-0000013"
    record = {
        "vehicle_id": vehicle_id,
        "actual_departure_time_seconds": 85.0,
        "departure_edge_id": "1390309919",
        "actual_departure_position_m": position_m,
        "actual_departure_lane_id": "1390309919_0",
        "source": "sumo_vehroute_resolved_departure",
        "source_checkpoint_id": checkpoint_id,
        "provenance_version": DEPARTURE_FRAGMENT_VERSION,
    }
    fragment = {
        "schema_version": 1,
        "provenance_version": DEPARTURE_FRAGMENT_VERSION,
        "checkpoint_id": checkpoint_id,
        "departed_vehicle_ids": [vehicle_id],
        "records": [record],
        "missing_numeric_vehicle_ids": [],
    }
    fragment["content_digest"] = hashlib.sha256(
        canonical_json(fragment).encode()
    ).hexdigest()
    write_native_departure_fragment(
        checkpoint / "departure-provenance-fragment.json", fragment
    )
    metadata_path = checkpoint / "checkpoint.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["departure_provenance_fragment"] = {
        "content_digest": fragment["content_digest"]
    }
    metadata_path.write_text(canonical_json(metadata) + "\n")


def _gzip_json(path: Path, value: dict[str, object]) -> None:
    _gzip_bytes(path, (canonical_json(value) + "\n").encode())


def _gzip_bytes(path: Path, value: bytes) -> None:
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as target,
    ):
        target.write(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
