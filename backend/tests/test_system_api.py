"""Phase 0 API and SQLite startup tests."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.core.settings import Settings
from backend.app.main import create_app


def _client(database_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=database_path,
        graph_path=database_path.parent / "missing.graphml",
        graph_manifest_path=database_path.parent / "missing-manifest.json",
    )
    return TestClient(create_app(settings))


def test_health_reports_api_and_database_ready(tmp_path: Path) -> None:
    database_path = tmp_path / "commute-help.db"

    with _client(database_path) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "application": "Commute Help API",
        "database": "ok",
    }
    assert database_path.exists()


def test_status_discloses_that_the_graph_is_not_configured(tmp_path: Path) -> None:
    with _client(tmp_path / "commute-help.db") as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["environment"] == "test"
    assert response.json()["graph"] == {
        "status": "not_configured",
        "version": None,
        "nodes": None,
        "directed_edges": None,
        "message": None,
    }


def test_graph_manifest_is_unavailable_before_phase_one_build(tmp_path: Path) -> None:
    with _client(tmp_path / "commute-help.db") as client:
        response = client.get("/api/graph/manifest")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "graph_unavailable"


def test_database_uses_wal_and_initializes_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "commute-help.db"

    with _client(database_path):
        pass

    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        schema_version = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert journal_mode == ("wal",)
    assert schema_version == ("2",)


def _scenario_content() -> dict:
    edge = {
        "edge_id": "fixture-edge",
        "u": "1",
        "v": "2",
        "key": 0,
        "road_name": "Fixture Road",
        "road_class": "primary",
        "osm_way_ids": ["123"],
        "geometry_fingerprint": "fixture-fingerprint",
        "lanes": 2,
        "maxspeed_kph": 50,
    }
    location = {
        "lat": 45.52,
        "lng": -122.68,
        "node_id": "1",
        "distance_m": 2,
        "label": "Fixture Road",
        "edge": edge,
    }
    return {
        "schema_version": 1,
        "graph_version": "fixture-v1",
        "origin": location,
        "destination": {**location, "lat": 45.53, "node_id": "2"},
        "departure_time": "2026-08-01T08:00:00-07:00",
        "closures": [
            {
                "selection": {
                    "graph_version": "fixture-v1",
                    "road_name": "Fixture Road",
                    "distance_m": 2,
                    "selected_edge_id": "fixture-edge",
                    "directions": [
                        {
                            "edge": edge,
                            "direction_label": "Northbound",
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [
                                    [-122.68, 45.52],
                                    [-122.67, 45.53],
                                ],
                            },
                        }
                    ],
                },
                "selected_edge_ids": ["fixture-edge"],
                "restriction": {"type": "full"},
            }
        ],
        "selected_route_id": None,
        "reliability_conditions": {
            "planning_mode": "depart_at",
            "arrival_deadline": None,
            "buffer_minutes": 8,
            "confidence_target": 0.9,
            "sample_count": 1000,
            "profile_id": None,
        },
        "diversion_conditions": {
            "demand_vph": 1200,
            "demand_pair_count": 20,
            "iterations": 4,
            "dispersion_radius_m": 500,
        },
    }


def test_scenario_crud_revisions_conflicts_and_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "commute-help.db"
    create_payload = {
        "name": "Morning closure",
        "content": _scenario_content(),
        "editor_name": "Test browser",
    }

    with _client(database_path) as client:
        created_response = client.post("/api/scenarios", json=create_payload)
        created = created_response.json()
        scenario_id = created["id"]
        listed = client.get("/api/scenarios").json()["scenarios"]
        loaded = client.get(f"/api/scenarios/{scenario_id}").json()
        updated_response = client.put(
            f"/api/scenarios/{scenario_id}",
            json={
                **create_payload,
                "name": "Renamed closure",
                "expected_revision": 1,
            },
        )
        conflict_response = client.put(
            f"/api/scenarios/{scenario_id}",
            json={**create_payload, "expected_revision": 1},
        )
        duplicate_response = client.post(
            f"/api/scenarios/{scenario_id}/duplicate",
            json={"name": "Teammate copy"},
        )
        export_response = client.get(f"/api/scenarios/{scenario_id}/export")
        import_response = client.post(
            "/api/scenarios/import",
            json={"scenario": export_response.json(), "name": "Imported copy"},
        )
        archive_response = client.delete(f"/api/scenarios/{scenario_id}")
        active_after_archive = client.get("/api/scenarios").json()["scenarios"]

    assert created_response.status_code == 201
    assert created["revision"] == 1
    assert created["content"] == loaded["content"]
    assert created["content"]["reliability_conditions"]["planning_mode"] == "depart_at"
    assert created["content"]["reliability_conditions"]["arrival_deadline"] is None
    assert created["content"]["diversion_conditions"] == {
        "demand_vph": 1200,
        "demand_pair_count": 20,
        "iterations": 4,
        "dispersion_radius_m": 500,
        "traffic_profile_id": None,
    }
    assert created["graph_status"] == "review_required"
    assert [item["id"] for item in listed] == [scenario_id]
    assert updated_response.status_code == 200
    assert updated_response.json()["revision"] == 2
    assert updated_response.json()["name"] == "Renamed closure"
    assert conflict_response.status_code == 409
    assert conflict_response.json()["detail"]["code"] == "scenario_revision_conflict"
    assert duplicate_response.status_code == 201
    assert duplicate_response.json()["id"] != scenario_id
    assert duplicate_response.json()["revision"] == 1
    assert export_response.status_code == 200
    assert "attachment" in export_response.headers["content-disposition"]
    assert import_response.status_code == 201
    assert import_response.json()["content"] == created["content"]
    assert archive_response.status_code == 204
    assert scenario_id not in {item["id"] for item in active_after_archive}

    with sqlite3.connect(database_path) as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM scenario_revisions WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()[0]
        import_count = connection.execute(
            "SELECT COUNT(*) FROM scenario_imports"
        ).fetchone()[0]
    assert revision_count == 2
    assert import_count == 1
