"""Phase 7 diversion assignment, caching, progress, and cancellation tests."""

from pathlib import Path
from threading import Event
import time

import networkx as nx
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from backend.app.core.settings import Settings
from backend.app.main import create_app
from backend.app.services.graph_build_service import (
    RegionDefinition,
    ValidationRoute,
    normalize_graph,
    validate_graph,
    write_graph_artifacts,
)


def _application(tmp_path: Path):
    graph = nx.MultiDiGraph(crs="EPSG:4326", simplified=True)
    nodes = {
        1: (-122.68, 45.51),
        2: (-122.67, 45.525),
        3: (-122.66, 45.53),
        4: (-122.67, 45.52),
        5: (-122.67, 45.515),
    }
    for node, (longitude, latitude) in nodes.items():
        graph.add_node(node, x=longitude, y=latitude)
    corridors = [
        (2, "primary", "Fast Corridor", 900.0),
        (4, "secondary", "Steady Corridor", 1000.0),
        (5, "residential", "Neighborhood Corridor", 1100.0),
    ]
    osmid = 500
    for middle, road_class, name, length in corridors:
        for u, v in [(1, middle), (middle, 1), (middle, 3), (3, middle)]:
            graph.add_edge(
                u,
                v,
                key=0,
                osmid=osmid,
                highway=road_class,
                name=name,
                oneway=False,
                length=length,
                geometry=LineString([nodes[u], nodes[v]]),
            )
            osmid += 1
    graph = normalize_graph(graph, "diversion-fixture-v1")
    region = RegionDefinition(
        id="diversion-fixture-region-v1",
        name="Diversion fixture region",
        geometry=box(-122.7, 45.49, -122.64, 45.55),
        validation_routes=(
            ValidationRoute(
                name="Fixture route",
                origin=(-122.68, 45.51),
                destination=(-122.66, 45.53),
            ),
        ),
    )
    graph_directory = tmp_path / "graphs"
    report = validate_graph(graph, region, "diversion-fixture-v1")
    write_graph_artifacts(
        graph,
        region,
        "diversion-fixture-v1",
        graph_directory,
        report,
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=graph_directory / "portland-vancouver.graphml",
        graph_manifest_path=graph_directory / "graph-manifest.json",
        traffic_path=tmp_path / "traffic",
    )
    return create_app(settings)


def _payload(application) -> dict:
    graph = application.state.graph_service.graph
    edge = graph.edges[1, 2, 0]
    return {
        "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
        "destination": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
        "graph_version": "diversion-fixture-v1",
        "departure_time": "2026-09-14T07:15:00-07:00",
        "closures": [
            {
                "type": "full",
                "edges": [
                    {
                        "edge_id": str(edge["edge_id"]),
                        "u": "1",
                        "v": "2",
                        "key": 0,
                    }
                ],
            }
        ],
        "demand_vph": 600,
        "demand_pair_count": 5,
        "iterations": 3,
        "dispersion_radius_m": 0,
        "max_result_edges": 50,
    }


def _wait_for_terminal(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/diversions/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("diversion job did not finish")


def test_diversion_changes_corridors_and_reuses_cache(tmp_path: Path) -> None:
    application = _application(tmp_path)
    with TestClient(application) as client:
        payload = _payload(application)
        started = client.post("/api/diversions", json=payload)
        finished = _wait_for_terminal(client, started.json()["id"])
        cached = client.post("/api/diversions", json=payload)

    assert started.status_code == 202
    assert finished["status"] == "completed", finished
    result = finished["result"]
    assert result["evidence_level"] == "modeled_uncalibrated"
    assert result["model_version"] == "background-flow-msa-bpr-v2"
    assert result["background_source"] == "Synthetic demand only"
    assert result["background_matched_edge_count"] == 0
    assert result["recommended_route"] is not None
    assert result["assigned_demand_vph"] == 600
    assert result["unassigned_demand_vph"] == 0
    assert result["max_increase_vph"] > 0
    assert result["max_decrease_vph"] < 0
    assert result["changed_edge_count"] >= 4
    assert any(
        change["road_name"] == "Fast Corridor" and change["change_vph"] < 0
        for change in result["edge_changes"]
    )
    assert any(
        change["road_name"] == "Steady Corridor" and change["change_vph"] > 0
        for change in result["edge_changes"]
    )
    assert all(len(change["geometry"]["coordinates"]) >= 2 for change in result["edge_changes"])
    assert cached.status_code == 202
    assert cached.json()["status"] == "completed"
    assert cached.json()["cached"] is True
    assert cached.json()["result"] == result


def test_observed_background_flow_is_displaced_and_routes_the_trip(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        edge_id = str(application.state.graph_service.graph.edges[1, 2, 0]["edge_id"])
        csv = (
            "station_or_segment_id,timestamp_local,speed_kph,volume,quality_flag\n"
            f"{edge_id},2025-09-08T07:15:00-07:00,32,900,good\n"
            f"{edge_id},2025-09-15T07:15:00-07:00,35,1000,good\n"
            f"{edge_id},2025-10-06T07:15:00-07:00,30,800,good\n"
        ).encode()
        imported = client.post(
            "/api/traffic/import",
            files={"file": ("portal-normalized.csv", csv, "text/csv")},
            data={"source_name": "PORTAL fixture"},
        )
        assert imported.status_code == 200, imported.text
        profile = imported.json()["profiles"][0]
        payload = {
            **_payload(application),
            "traffic_profile_id": profile["id"],
        }
        started = client.post("/api/diversions", json=payload)
        finished = _wait_for_terminal(client, started.json()["id"])

    assert finished["status"] == "completed", finished
    result = finished["result"]
    assert result["background_source"] == "PORTAL fixture"
    assert result["background_bucket"] == "07:15 Pacific weekday"
    assert result["background_observation_count"] == 3
    assert result["background_matched_edge_count"] == 1
    assert result["background_network_coverage_percent"] > 0
    assert result["displaced_background_vph"] == 900
    assert result["assigned_demand_vph"] == 1500
    assert result["recommended_route"]["travel_time_seconds"] > 0
    closed_change = next(
        change for change in result["edge_changes"] if change["edge_id"] == edge_id
    )
    assert closed_change["baseline_vph"] >= 900
    assert closed_change["scenario_vph"] == 0


def test_queued_diversion_can_be_cancelled(tmp_path: Path) -> None:
    application = _application(tmp_path)
    blocker = Event()
    with TestClient(application) as client:
        application.state.diversion_service._executor.submit(blocker.wait)
        started = client.post("/api/diversions", json=_payload(application)).json()
        cancelled = client.delete(f"/api/diversions/{started['id']}")
        blocker.set()
        terminal = _wait_for_terminal(client, started["id"])

    assert cancelled.status_code == 200
    assert terminal["status"] == "cancelled"
    assert terminal["result"] is None


def test_diversion_rejects_graph_version_mismatch(tmp_path: Path) -> None:
    application = _application(tmp_path)
    with TestClient(application) as client:
        payload = {**_payload(application), "graph_version": "old-graph"}
        response = client.post("/api/diversions", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "closure_edge_mismatch"
