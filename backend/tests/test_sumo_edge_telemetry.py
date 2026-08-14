"""Phase 2.1 native SUMO edge-telemetry contract and materialization tests."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from backend.app.schemas.simulation import SimulationRunRequest
from backend.app.schemas.sumo_edge_telemetry import SumoEdgeTelemetryRecordV1
from backend.app.services.sumo.edge_telemetry_service import (
    NATIVE_DEFINITION,
    NATIVE_FRAGMENT,
    TELEMETRY_DIRECTORY,
    TELEMETRY_MANIFEST,
    TELEMETRY_PARQUET,
    SumoEdgeTelemetryError,
    collect_checkpoint_fragments,
    materialize_sumo_edge_telemetry,
    validate_native_fragment,
    write_native_edge_data_definition,
)
from backend.app.workers.regional_sumo_worker import _write_playback_from_chunks
from scripts.inspect_sumo_edge_telemetry import inspect_telemetry


def test_telemetry_request_is_optional_and_only_accepts_900_seconds() -> None:
    assert SimulationRunRequest().edge_telemetry_interval_seconds is None
    enabled = SimulationRunRequest(edge_telemetry_interval_seconds=900)
    assert enabled.edge_telemetry_interval_seconds == 900
    with pytest.raises(ValidationError):
        SimulationRunRequest(edge_telemetry_interval_seconds=600)


def test_record_requires_exact_interval_explicit_units_and_nonnegative_counts() -> None:
    record = SumoEdgeTelemetryRecordV1.model_validate(_record())
    assert record.interval_end_seconds - record.interval_start_seconds == 900
    assert record.flow_vph == 40
    assert record.mean_speed_mps == 10
    assert record.mean_speed_kph == 36
    assert record.sumo_edge_id == "native-edge"
    with pytest.raises(ValidationError):
        SumoEdgeTelemetryRecordV1.model_validate(
            {**_record(), "entered_count": -1, "flow_vph": -4}
        )
    with pytest.raises(ValidationError):
        SumoEdgeTelemetryRecordV1.model_validate(
            {**_record(), "interval_end_seconds": 899}
        )


def test_native_definition_uses_edge_data_and_checkpoint_local_semantics(
    tmp_path: Path,
) -> None:
    definition = tmp_path / NATIVE_DEFINITION
    output = tmp_path / NATIVE_FRAGMENT
    write_native_edge_data_definition(
        definition_path=definition,
        output_path=output,
        begin_seconds=0,
        end_seconds=100,
        include_empty_edge_catalog=True,
    )
    value = definition.read_text(encoding="utf-8")
    assert "<edgeData" in value
    assert 'period="900"' in value
    assert 'begin="0"' in value
    assert 'end="100"' in value
    assert 'excludeEmpty="false"' in value
    assert 'withInternal="false"' in value
    assert "sampledSeconds" in value
    assert "waitingTime" in value
    assert "timeLoss" in value


def test_installed_sumo_emits_native_mesoscopic_edge_data(tmp_path: Path) -> None:
    binary_dir = Path(sys.executable).parent
    sumo = binary_dir / "sumo"
    netconvert = binary_dir / "netconvert"
    if not sumo.is_file() or not netconvert.is_file():
        pytest.skip("Local SUMO runtime is not installed")
    fixture_dir = Path("backend/tests/fixtures/sumo").resolve()
    network = tmp_path / "tiny.net.xml"
    subprocess.run(
        [
            str(netconvert),
            "--node-files",
            str(fixture_dir / "tiny.nod.xml"),
            "--edge-files",
            str(fixture_dir / "tiny.edg.xml"),
            "--output-file",
            str(network),
            "--no-warnings",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    routes = tmp_path / "tiny.rou.xml"
    routes.write_text(
        """<routes>
  <vType id="passenger" vClass="passenger"/>
  <vehicle id="probe" type="passenger" depart="0">
    <route edges="A_B B_C C_F"/>
  </vehicle>
</routes>
""",
        encoding="utf-8",
    )
    definition = tmp_path / NATIVE_DEFINITION
    output = tmp_path / NATIVE_FRAGMENT
    write_native_edge_data_definition(
        definition_path=definition,
        output_path=output,
        begin_seconds=0,
        end_seconds=100,
        include_empty_edge_catalog=True,
    )
    subprocess.run(
        [
            str(sumo),
            "--net-file",
            str(network),
            "--route-files",
            str(routes),
            "--additional-files",
            str(definition),
            "--mesosim",
            "true",
            "--begin",
            "0",
            "--end",
            "100",
            "--no-step-log",
            "true",
            "--no-warnings",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert validate_native_fragment(
        output, expected_begin_seconds=0, expected_end_seconds=100
    ) >= 3
    with gzip.open(output, "rb") as source:
        root = ET.parse(source).getroot()
    probe_edges = [
        edge
        for edge in root.findall("./interval/edge")
        if float(edge.attrib.get("sampledSeconds", 0)) > 0
    ]
    assert probe_edges
    assert all("speed" in edge.attrib for edge in probe_edges)
    assert all("entered" in edge.attrib for edge in probe_edges)


def test_native_fragments_materialize_dense_deterministic_900_second_rows(
    tmp_path: Path,
) -> None:
    first = _artifact_fixture(tmp_path / "first", end_seconds=1800)
    second = _artifact_fixture(tmp_path / "second", end_seconds=1800)
    assert first.output.row_count == 8
    assert first.output.sha256 == second.output.sha256
    assert first.row_content_sha256 == second.row_content_sha256
    assert first.content_digest == second.content_digest
    resumed = _materialize(tmp_path / "first", end_seconds=1800)
    assert resumed.content_digest == first.content_digest
    assert resumed.output.sha256 == first.output.sha256

    table = pq.read_table(
        tmp_path / "first" / TELEMETRY_DIRECTORY / TELEMETRY_PARQUET
    ).to_pandas()
    assert list(table.columns) == [field.name for field in table_schema()]
    assert not table.duplicated(
        ["variant", "sumo_edge_id", "interval_start_seconds"]
    ).any()
    assert set(table["variant"]) == {"baseline", "scenario"}
    assert set(table["interval_start_seconds"]) == {0, 900}
    assert set(table["interval_end_seconds"]) == {900, 1800}
    assert set(table["interval_duration_seconds"]) == {900}

    active = table.loc[
        (table["variant"] == "baseline")
        & (table["sumo_edge_id"] == "native-edge")
        & (table["interval_start_seconds"] == 0)
    ].iloc[0]
    assert active["entered_count"] == 10
    assert active["flow_vph"] == 40
    assert active["departed_count"] == 1
    assert active["left_count"] == 8
    assert active["sampled_vehicle_seconds"] == 300
    assert active["mean_speed_mps"] == 10
    assert active["mean_speed_kph"] == 36
    assert active["mean_travel_time_seconds"] == 10
    assert active["density_veh_per_km"] == 1
    assert active["occupancy_percent"] == 2
    assert active["waiting_time_seconds"] == 6
    assert active["time_loss_seconds"] == 9

    empty = table.loc[
        (table["variant"] == "baseline")
        & (table["sumo_edge_id"] == "zero-edge")
        & (table["interval_start_seconds"] == 0)
    ].iloc[0]
    assert empty["entered_count"] == 0
    assert empty["flow_vph"] == 0
    assert empty["sampled_vehicle_seconds"] == 0
    assert empty["mean_speed_mps"] != empty["mean_speed_mps"]
    assert empty["occupancy_percent"] != empty["occupancy_percent"]

    summary = inspect_telemetry(tmp_path / "first")
    assert summary["telemetry_row_count"] == 8
    assert summary["interval_count"] == 2
    assert summary["distinct_edge_count"] == 2
    assert summary["duplicate_edge_interval_identities"] == 0
    assert summary["deterministic_order_violations"] == 0
    assert not any(summary["negative_value_counts"].values())


def test_partial_interval_is_not_promoted_as_a_complete_record(tmp_path: Path) -> None:
    manifest = _artifact_fixture(tmp_path / "partial", end_seconds=1200)
    assert manifest.interval.complete_interval_count_per_variant == 1
    assert manifest.interval.omitted_partial_seconds_at_end == 300
    assert manifest.output.row_count == 4
    table = pq.read_table(
        tmp_path / "partial" / TELEMETRY_DIRECTORY / TELEMETRY_PARQUET
    )
    assert set(table.column("interval_start_seconds").to_pylist()) == {0}


def test_negative_native_metric_fails_without_authoritative_promotion(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "negative"
    _write_fixture_inputs(run_dir)
    for variant in ("baseline", "scenario"):
        _write_checkpoint(
            run_dir,
            variant=variant,
            begin=0,
            end=900,
            edges=[
                {"id": "native-edge", "entered": "-1"},
                {"id": "zero-edge", "entered": "0"},
            ],
            catalog=True,
        )
    with pytest.raises(SumoEdgeTelemetryError, match="must be nonnegative"):
        _materialize(run_dir, end_seconds=900)
    assert not (run_dir / TELEMETRY_DIRECTORY).exists()


def test_interrupted_or_conflicting_fragments_cannot_promote(tmp_path: Path) -> None:
    run_dir = tmp_path / "interrupted"
    _write_fixture_inputs(run_dir)
    _write_checkpoint(
        run_dir,
        variant="baseline",
        begin=0,
        end=900,
        edges=[{"id": "native-edge", "entered": "1"}],
        catalog=True,
    )
    _write_checkpoint(
        run_dir,
        variant="scenario",
        begin=0,
        end=900,
        edges=[
            {"id": "native-edge", "entered": "1"},
            {"id": "native-edge", "entered": "1"},
        ],
        catalog=True,
    )
    with pytest.raises(SumoEdgeTelemetryError, match="repeats SUMO edge ID"):
        _materialize(run_dir, end_seconds=900)
    assert not (run_dir / TELEMETRY_DIRECTORY / TELEMETRY_MANIFEST).exists()


def test_resume_fragment_discovery_ignores_temporary_output_and_has_no_duplicates(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "resume"
    _write_checkpoint(
        run_dir,
        variant="baseline",
        begin=0,
        end=900,
        edges=[{"id": "native-edge", "entered": "1"}],
        catalog=True,
    )
    temporary = run_dir / "checkpoints" / "baseline" / "00001800.tmp"
    temporary.mkdir(parents=True)
    fragments = collect_checkpoint_fragments(run_dir, "baseline")
    assert [(item.begin_seconds, item.end_seconds) for item in fragments] == [(0, 900)]


def test_playback_writer_remains_independent_from_telemetry(tmp_path: Path) -> None:
    chunk = tmp_path / "frame.jsonl.gz"
    with gzip.GzipFile(filename=str(chunk), mode="wb", mtime=0) as output:
        output.write(b'{"elapsed_seconds":0,"agents":[]}\n')
    playback = tmp_path / "playback.json"
    _write_playback_from_chunks(
        playback,
        {
            "schema_version": 1,
            "duration_seconds": 900,
            "frame_interval_seconds": 15,
            "seed": 1,
            "real_vehicles_per_simulated_vehicle": 1,
            "displayed_vehicle_limit": 10,
        },
        [chunk],
    )
    value = json.loads(playback.read_text(encoding="utf-8"))
    assert value["frames"] == [{"elapsed_seconds": 0, "agents": []}]


def table_schema():
    from backend.app.services.sumo.edge_telemetry_service import TELEMETRY_ARROW_SCHEMA

    return TELEMETRY_ARROW_SCHEMA


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_identity": "run-key",
        "network_version": "network-v1",
        "sumo_version": "1.27.1",
        "simulation_mode": "mesoscopic",
        "seed": 42,
        "demand_version": "demand-v1",
        "variant": "baseline",
        "sumo_edge_id": "native-edge",
        "interval_start_seconds": 0,
        "interval_end_seconds": 900,
        "interval_duration_seconds": 900,
        "entered_count": 10,
        "departed_count": 1,
        "left_count": 8,
        "arrived_count": 1,
        "flow_vph": 40,
        "flow_derivation": "entered_count * 3600 / interval_duration_seconds",
        "sampled_vehicle_seconds": 100,
        "mean_speed_mps": 10,
        "mean_speed_kph": 36,
        "mean_travel_time_seconds": 10,
        "density_veh_per_km": 1,
        "occupancy_percent": 2,
        "waiting_time_seconds": 3,
        "time_loss_seconds": 4,
    }


def _artifact_fixture(run_dir: Path, *, end_seconds: int):
    _write_fixture_inputs(run_dir)
    for variant in ("baseline", "scenario"):
        for begin in range(0, end_seconds, 300):
            end = min(begin + 300, end_seconds)
            interval_offset = begin % 900
            active = interval_offset < 900
            edges = [
                {
                    "id": "native-edge",
                    "sampledSeconds": "100",
                    "traveltime": "10",
                    "density": "1",
                    "occupancy": "2",
                    "waitingTime": "2",
                    "timeLoss": "3",
                    "speed": "10",
                    "departed": "1" if interval_offset == 0 else "0",
                    "arrived": "1" if interval_offset == 600 else "0",
                    "entered": "10" if interval_offset == 0 else "0",
                    "left": "8" if interval_offset == 600 else "0",
                }
            ]
            if begin == 0:
                edges.append(
                    {
                        "id": "zero-edge",
                        "departed": "0",
                        "arrived": "0",
                        "entered": "0",
                        "left": "0",
                    }
                )
            assert active
            _write_checkpoint(
                run_dir,
                variant=variant,
                begin=begin,
                end=end,
                edges=edges,
                catalog=(begin == 0),
            )
    return _materialize(run_dir, end_seconds=end_seconds)


def _write_fixture_inputs(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "network-manifest.json").write_text(
        json.dumps(
            {
                "network_version": "fixture-network-v1",
                "sumo_version": "1.27.1",
                "artifact": {"sha256": "a" * 64},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "graph-manifest.json").write_text(
        json.dumps({"graph_version": "fixture-graph-v1"}), encoding="utf-8"
    )


def _write_checkpoint(
    run_dir: Path,
    *,
    variant: str,
    begin: int,
    end: int,
    edges: list[dict[str, str]],
    catalog: bool,
) -> None:
    checkpoint = run_dir / "checkpoints" / variant / f"{end:08d}"
    checkpoint.mkdir(parents=True, exist_ok=True)
    xml = [
        "<meandata>",
        f'  <interval begin="{begin}" end="{end}" id="fixture">',
        *[
            "    <edge "
            + " ".join(f'{name}="{value}"' for name, value in edge.items())
            + "/>"
            for edge in edges
        ],
        "  </interval>",
        "</meandata>",
    ]
    with gzip.GzipFile(
        filename=str(checkpoint / NATIVE_FRAGMENT), mode="wb", mtime=0
    ) as output:
        output.write(("\n".join(xml) + "\n").encode("utf-8"))
    (checkpoint / "checkpoint.json").write_text(
        json.dumps(
            {
                "completed": True,
                "variant": variant,
                "chunk_start_second": begin,
                "chunk_end_second": end,
                "telemetry_interval_seconds": 900,
                "telemetry_native_edge_count": len(edges),
                "telemetry_edge_catalog_included": catalog,
            }
        ),
        encoding="utf-8",
    )


def _materialize(run_dir: Path, *, end_seconds: int):
    return materialize_sumo_edge_telemetry(
        run_dir=run_dir,
        application_run_id="fixture-application-run",
        run_identity="fixture-deterministic-run",
        network_manifest_path=run_dir / "network-manifest.json",
        graph_manifest_path=run_dir / "graph-manifest.json",
        demand_manifest={
            "demand_version": "fixture-demand-v1",
            "artifacts": {"routes": {"sha256": "b" * 64}},
        },
        seed=42,
        real_vehicles_per_simulated_vehicle=1,
        simulation_start_seconds=0,
        simulation_end_seconds=end_seconds,
        interval_seconds=900,
    )
