"""Focused SUMO Phase 0 runtime and API tests."""

import shutil
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.core.settings import Settings
from backend.app.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=tmp_path / "missing.graphml",
        graph_manifest_path=tmp_path / "missing-manifest.json",
        sumo_network_manifest_path=tmp_path / "missing-sumo-manifest.json",
        traffic_schedule_path=tmp_path / "missing-schedule.parquet",
        traffic_schedule_manifest_path=tmp_path / "missing-schedule-manifest.json",
        proxy_od_seed_path=tmp_path / "missing-proxy-od.parquet",
        proxy_od_report_path=tmp_path / "missing-proxy-report.json",
        sumo_active_model_manifest_path=tmp_path / "missing-model-manifest.json",
    )


def test_simulation_status_reports_local_runtime(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/api/simulation/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["sumo_version"] == "1.27.1"
    assert payload["runtime_mode"] in {"libsumo", "subprocess"}
    assert payload["netconvert_available"] is True
    assert payload["network_ready"] is False
    assert payload["schedule_ready"] is False
    assert payload["proxy_demand_ready"] is False
    assert payload["model_ready"] is False
    assert payload["offline_only"] is True


def test_tiny_fixture_compiles_and_loads(tmp_path: Path) -> None:
    netconvert = shutil.which("netconvert") or str(Path(sys.executable).parent / "netconvert")
    sumo = shutil.which("sumo") or str(Path(sys.executable).parent / "sumo")
    assert Path(netconvert).is_file()
    assert Path(sumo).is_file()
    fixture = Path(__file__).parent / "fixtures" / "sumo"
    network = tmp_path / "tiny.net.xml"

    build = subprocess.run(
        [
            netconvert,
            "--node-files",
            str(fixture / "tiny.nod.xml"),
            "--edge-files",
            str(fixture / "tiny.edg.xml"),
            "--output-file",
            str(network),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    load = subprocess.run(
        [
            sumo,
            "--net-file",
            str(network),
            "--begin",
            "0",
            "--end",
            "1",
            "--no-step-log",
            "true",
            "--duration-log.disable",
            "true",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert load.returncode == 0, load.stderr
