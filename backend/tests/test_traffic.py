"""Phase 6 traffic import, calibration, and reliability tests."""

from pathlib import Path

import networkx as nx
import pandas as pd
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


def _client(tmp_path: Path) -> TestClient:
    graph = nx.MultiDiGraph(crs="EPSG:4326", simplified=True)
    nodes = {
        1: (-122.68, 45.51),
        2: (-122.67, 45.52),
        3: (-122.66, 45.53),
    }
    for node, (longitude, latitude) in nodes.items():
        graph.add_node(node, x=longitude, y=latitude)
    for u, v, osmid in [(1, 2, 101), (2, 1, 101), (2, 3, 102), (3, 2, 102)]:
        graph.add_edge(
            u,
            v,
            key=0,
            osmid=osmid,
            highway="primary",
            name="Fixture Street",
            oneway=False,
            maxspeed="50",
            length=1400.0,
            geometry=LineString([nodes[u], nodes[v]]),
        )
    graph = normalize_graph(graph, "traffic-fixture-v1")
    region = RegionDefinition(
        id="traffic-fixture-region-v1",
        name="Traffic fixture region",
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
    report = validate_graph(graph, region, "traffic-fixture-v1")
    write_graph_artifacts(
        graph,
        region,
        "traffic-fixture-v1",
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
    return TestClient(create_app(settings))


def _route() -> dict:
    return {
        "route_id": "fixture-route-v1",
        "travel_time_seconds": 1200,
        "distance_m": 15000,
        "geometry": {
            "type": "LineString",
            "coordinates": [[-122.68, 45.51], [-122.66, 45.53]],
        },
    }


def test_import_builds_versioned_profiles_and_reports_quality(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        selection = client.post(
            "/api/roads/select",
            json={"lat": 45.515, "lng": -122.675},
        ).json()
        edge_id = selection["location"]["edge"]["edge_id"]
        csv = (
            "station_or_segment_id,timestamp_local,timezone,direction,volume,speed_kph,quality_flag\n"
            f"{edge_id},2025-09-08T07:00:00,America/Los_Angeles,NB,900,25,good\n"
            f"{edge_id},2025-10-06T08:00:00,America/Los_Angeles,NB,850,40,good\n"
            f"{edge_id},2025-09-08T16:00:00,America/Los_Angeles,NB,1100,20,good\n"
            f"{edge_id},2025-10-06T17:00:00,America/Los_Angeles,NB,,30,reviewed\n"
            "unmatched,not-a-date,America/Los_Angeles,NB,100,0,bad\n"
        ).encode()

        response = client.post(
            "/api/traffic/import",
            files={"file": ("observations.csv", csv, "text/csv")},
            data={"source_name": "Fixture DOT observations"},
        )
        listing = client.get("/api/traffic/profiles")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["quality"] == {
        "row_count": 5,
        "accepted_count": 4,
        "rejected_count": 1,
        "matched_station_count": 1,
        "unmatched_station_count": 1,
        "missing_speed_percent": 20.0,
        "missing_volume_percent": 20.0,
        "quality_flags": ["bad", "good", "reviewed"],
    }
    assert {profile["period"] for profile in payload["profiles"]} == {
        "weekday_morning",
        "weekday_afternoon",
    }
    assert all(profile["observation_count"] == 2 for profile in payload["profiles"])
    assert all("2025-" in profile["source_window"] for profile in payload["profiles"])
    assert listing.status_code == 200
    assert len(listing.json()["profiles"]) == 2
    assert len(list((tmp_path / "traffic" / "normalized").glob("*.parquet"))) == 1
    assert payload["normalized_path"].startswith("normalized/")
    raw_files = list((tmp_path / "traffic" / "raw").glob("*/observations.csv"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == csv
    assert (tmp_path / "traffic" / "traffic-profile-manifest.json").exists()


def test_historical_and_modeled_simulations_are_reproducible(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        selection = client.post(
            "/api/roads/select",
            json={"lat": 45.515, "lng": -122.675},
        ).json()
        edge_id = selection["location"]["edge"]["edge_id"]
        csv = (
            "station_or_segment_id,timestamp_local,speed_kph,volume\n"
            f"{edge_id},2025-09-08T07:00:00-07:00,25,900\n"
            f"{edge_id},2025-09-09T07:30:00-07:00,35,800\n"
            f"{edge_id},2025-10-06T08:00:00-07:00,45,700\n"
        ).encode()
        imported = client.post(
            "/api/traffic/import",
            files={"file": ("calibration.csv", csv, "text/csv")},
            data={"source_name": "Fixture observations"},
        ).json()
        profile_id = imported["profiles"][0]["id"]
        request = {
            "route": _route(),
            "departure_time": "2026-09-14T07:15:00-07:00",
            "arrival_deadline": "2026-09-14T08:15:00-07:00",
            "buffer_minutes": 8,
            "confidence_target": 0.9,
            "sample_count": 1000,
            "profile_id": profile_id,
        }
        historical_first = client.post("/api/simulations", json=request)
        historical_second = client.post("/api/simulations", json=request)
        modeled = client.post(
            "/api/simulations",
            json={**request, "profile_id": None},
        )
        departure_mode = client.post(
            "/api/simulations",
            json={
                **request,
                "planning_mode": "depart_at",
                "arrival_deadline": None,
                "profile_id": None,
            },
        )
        missing_arrival_deadline = client.post(
            "/api/simulations",
            json={**request, "arrival_deadline": None},
        )

    assert historical_first.status_code == 200, historical_first.text
    assert historical_first.json() == historical_second.json()
    result = historical_first.json()
    assert result["evidence_level"] == "historically_calibrated"
    assert result["source_name"] == "Fixture observations"
    assert result["sample_count"] == 1000
    assert result["median_seconds"] <= result["p85_seconds"] <= result["p90_seconds"] <= result["p95_seconds"]
    assert result["latest_safe_departure"].endswith("-07:00")
    assert len(result["early_departure_benefits"]) == 3
    assert modeled.status_code == 200
    assert modeled.json()["evidence_level"] == "modeled_uncalibrated"
    assert modeled.json()["source_window"] == "No historical observations"
    assert departure_mode.status_code == 200
    departure_result = departure_mode.json()
    assert departure_result["planning_mode"] == "depart_at"
    assert departure_result["on_time_probability"] is None
    assert departure_result["latest_safe_departure"] is None
    assert departure_result["early_departure_benefits"] == []
    assert departure_result["confidence_arrival_time"] == departure_result["p90_arrival_time"]
    assert missing_arrival_deadline.status_code == 422


def test_coordinate_matching_preserves_opposing_directions(tmp_path: Path) -> None:
    csv = (
        "station_or_segment_id,timestamp_local,direction,speed_kph,latitude,longitude\n"
        "sensor-north,2025-09-08T07:00:00-07:00,NE,30,45.515,-122.675\n"
        "sensor-south,2025-09-08T07:05:00-07:00,SW,30,45.515,-122.675\n"
    ).encode()
    with _client(tmp_path) as client:
        response = client.post(
            "/api/traffic/import",
            files={"file": ("directions.csv", csv, "text/csv")},
            data={"source_name": "Directional fixture"},
        )

    assert response.status_code == 200, response.text
    normalized_path = next((tmp_path / "traffic" / "normalized").glob("*.parquet"))
    normalized = pd.read_parquet(normalized_path)
    assert normalized["edge_id"].nunique() == 2
    assert set(normalized["match_confidence"]) == {"high"}


def test_travel_time_observations_can_calibrate_without_speed(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        selection = client.post(
            "/api/roads/select",
            json={"lat": 45.515, "lng": -122.675},
        ).json()
        edge_id = selection["location"]["edge"]["edge_id"]
        csv = (
            "station_or_segment_id,timestamp_local,travel_time_seconds,volume\n"
            f"{edge_id},2025-09-08T07:00:00-07:00,180,700\n"
            f"{edge_id},2025-10-06T08:00:00-07:00,240,800\n"
        ).encode()
        response = client.post(
            "/api/traffic/import",
            files={"file": ("travel-times.csv", csv, "text/csv")},
            data={"source_name": "Travel-time fixture"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["quality"]["accepted_count"] == 2
    assert payload["quality"]["missing_speed_percent"] == 100.0
    assert payload["profiles"][0]["observation_count"] == 2


def test_invalid_import_is_rejected_without_database_record(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/traffic/import",
            files={"file": ("invalid.csv", b"name,value\nroad,1\n", "text/csv")},
            data={"source_name": "Invalid fixture"},
        )
        profiles = client.get("/api/traffic/profiles")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_traffic_import"
    assert profiles.json() == {"profiles": []}
