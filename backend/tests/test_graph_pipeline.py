"""Phase 1 graph normalization, validation, artifact, and loading tests."""

import hashlib
from pathlib import Path

import networkx as nx
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from backend.app.core.settings import Settings
from backend.app.main import create_app
from backend.app.services.graph_build_service import (
    RegionDefinition,
    ValidationRoute,
    normalize_graph,
    refresh_manifest_validation,
    validate_graph,
    write_graph_artifacts,
)


def _synthetic_graph() -> nx.MultiDiGraph:
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
            highway="residential",
            name="Fixture Street",
            oneway=False,
            length=1400.0,
            geometry=LineString([nodes[u], nodes[v]]),
        )
    return graph


def _alternative_graph() -> nx.MultiDiGraph:
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
        (5, "residential", "Neighborhood Corridor", 700.0),
    ]
    osmid = 200
    for middle, highway, name, length in corridors:
        for u, v in [(1, middle), (middle, 1), (middle, 3), (3, middle)]:
            graph.add_edge(
                u,
                v,
                key=0,
                osmid=osmid,
                highway=highway,
                name=name,
                oneway=False,
                length=length,
                geometry=LineString([nodes[u], nodes[v]]),
            )
            osmid += 1
    return graph


def _region() -> RegionDefinition:
    return RegionDefinition(
        id="fixture-region-v1",
        name="Fixture region",
        geometry=box(-122.7, 45.49, -122.64, 45.55),
        validation_routes=(
            ValidationRoute(
                name="Fixture cross-region route",
                origin=(-122.68, 45.51),
                destination=(-122.66, 45.53),
            ),
        ),
    )


def test_normalization_preserves_direction_and_adds_routing_fields() -> None:
    graph = normalize_graph(_synthetic_graph(), "fixture-v1")

    forward = graph.edges[1, 2, 0]
    reverse = graph.edges[2, 1, 0]

    assert forward["edge_id"] != reverse["edge_id"]
    assert forward["lanes"] == 1
    assert forward["estimated_capacity_vph"] == 700
    assert forward["free_flow_seconds"] > 0
    assert len(forward["geometry_fingerprint"]) == 64


def test_validation_and_runtime_loading_use_generated_artifacts(tmp_path: Path) -> None:
    graph = normalize_graph(_synthetic_graph(), "fixture-v1")
    region = _region()
    report = validate_graph(graph, region, "fixture-v1")
    output_dir = tmp_path / "graphs"
    manifest = write_graph_artifacts(
        graph,
        region,
        "fixture-v1",
        output_dir,
        report,
    )

    assert report["gate_passed"] is True
    assert report["representative_routes"][0]["success"] is True
    assert manifest.validation_passed is True

    refreshed_manifest = refresh_manifest_validation(
        report,
        output_dir / "validation-report.json",
        output_dir / "graph-manifest.json",
    )
    report_hash = hashlib.sha256(
        (output_dir / "validation-report.json").read_bytes()
    ).hexdigest()
    assert refreshed_manifest.artifacts["validation_report"].sha256 == report_hash

    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=output_dir / "portland-vancouver.graphml",
        graph_manifest_path=output_dir / "graph-manifest.json",
    )
    application = create_app(settings)
    with TestClient(application) as client:
        status_response = client.get("/api/status")
        manifest_response = client.get("/api/graph/manifest")
        selection_response = client.post(
            "/api/roads/select",
            json={"lat": 45.515, "lng": -122.675},
        )
        closure_selection_response = client.post(
            "/api/roads/select-closure",
            json={"lat": 45.515, "lng": -122.675},
        )
        route_response = client.post(
            "/api/routes/compare",
            json={
                "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
                "destination": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
            },
        )
        same_location_response = client.post(
            "/api/routes/compare",
            json={
                "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
                "destination": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
            },
        )
        astar_path = application.state.routing_service.find_path(
            application.state.graph_service.graph,
            1,
            3,
        )
        dijkstra_path = nx.dijkstra_path(
            application.state.graph_service.graph,
            1,
            3,
            weight="routing_cost_seconds",
        )

    assert status_response.status_code == 200
    assert status_response.json()["graph"] == {
        "status": "ready",
        "version": "fixture-v1",
        "nodes": 3,
        "directed_edges": 4,
        "message": None,
    }
    assert manifest_response.status_code == 200
    assert manifest_response.json()["graph_version"] == "fixture-v1"
    assert selection_response.status_code == 200
    assert selection_response.json()["location"]["label"] == "Fixture Street"
    assert selection_response.json()["location"]["distance_m"] < 10
    assert closure_selection_response.status_code == 200
    closure_directions = closure_selection_response.json()["directions"]
    assert {item["direction_label"] for item in closure_directions} == {
        "Northeastbound",
        "Southwestbound",
    }
    assert route_response.status_code == 200
    route_payload = route_response.json()
    assert route_payload["evidence_level"] == "free_flow"
    assert route_payload["baseline"]["distance_m"] == 2800
    assert route_payload["baseline"]["travel_time_seconds"] > 0
    assert route_payload["baseline"]["geometry"]["coordinates"][0] == [
        -122.68,
        45.51,
    ]
    assert route_payload["baseline"]["geometry"]["coordinates"][-1] == [
        -122.66,
        45.53,
    ]
    assert same_location_response.status_code == 400
    assert same_location_response.json()["detail"]["code"] == "same_location"
    assert astar_path == dijkstra_path


def test_full_closures_apply_only_to_selected_direction(tmp_path: Path) -> None:
    graph = normalize_graph(_synthetic_graph(), "closure-fixture-v1")
    graph.edges[1, 2, 0]["lanes"] = 2.0
    region = _region()
    report = validate_graph(graph, region, "closure-fixture-v1")
    output_dir = tmp_path / "graphs"
    write_graph_artifacts(graph, region, "closure-fixture-v1", output_dir, report)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=output_dir / "portland-vancouver.graphml",
        graph_manifest_path=output_dir / "graph-manifest.json",
    )

    with TestClient(create_app(settings)) as client:
        selection = client.post(
            "/api/roads/select-closure",
            json={"lat": 45.515, "lng": -122.675},
        ).json()
        directions = {
            (item["edge"]["u"], item["edge"]["v"]): item["edge"]
            for item in selection["directions"]
        }
        second_selection = client.post(
            "/api/roads/select-closure",
            json={"lat": 45.525, "lng": -122.665},
        ).json()
        second_directions = {
            (item["edge"]["u"], item["edge"]["v"]): item["edge"]
            for item in second_selection["directions"]
        }
        request = {
            "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
            "destination": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
            "graph_version": "closure-fixture-v1",
        }
        reverse_only = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [{"type": "full", "edges": [directions[("2", "1")]]}],
            },
        )
        forward_only = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [{"type": "full", "edges": [directions[("1", "2")]]}],
            },
        )
        multiple_reverse_sections = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [
                    {
                        "type": "full",
                        "edges": [
                            directions[("2", "1")],
                            second_directions[("3", "2")],
                        ],
                    }
                ],
            },
        )
        lane_restriction = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [
                    {
                        "type": "lane",
                        "remaining_lanes": 1,
                        "edges": [directions[("1", "2")]],
                    }
                ],
            },
        )
        speed_restriction = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [
                    {
                        "type": "speed",
                        "speed_limit_kph": 10,
                        "edges": [directions[("1", "2")]],
                    }
                ],
            },
        )
        ineffective_lane_restriction = client.post(
            "/api/routes/compare",
            json={
                **request,
                "closures": [
                    {
                        "type": "lane",
                        "remaining_lanes": 2,
                        "edges": [directions[("1", "2")]],
                    }
                ],
            },
        )
        scheduled_active = client.post(
            "/api/routes/compare",
            json={
                **request,
                "departure_time": "2026-08-01T08:30:00-07:00",
                "closures": [
                    {
                        "type": "full",
                        "starts_at": "2026-08-01T08:00:00-07:00",
                        "ends_at": "2026-08-01T09:00:00-07:00",
                        "edges": [directions[("1", "2")]],
                    }
                ],
            },
        )
        scheduled_at_end_boundary = client.post(
            "/api/routes/compare",
            json={
                **request,
                "departure_time": "2026-08-01T09:00:00-07:00",
                "closures": [
                    {
                        "type": "full",
                        "starts_at": "2026-08-01T08:00:00-07:00",
                        "ends_at": "2026-08-01T09:00:00-07:00",
                        "edges": [directions[("1", "2")]],
                    }
                ],
            },
        )

    assert reverse_only.status_code == 200
    reverse_payload = reverse_only.json()
    assert reverse_payload["scenario_status"] == "available"
    assert reverse_payload["scenario"]["route_id"] == reverse_payload["baseline"][
        "route_id"
    ]
    assert forward_only.status_code == 200
    forward_payload = forward_only.json()
    assert forward_payload["scenario_status"] == "no_route"
    assert forward_payload["scenario"] is None
    assert multiple_reverse_sections.status_code == 200
    multiple_payload = multiple_reverse_sections.json()
    assert multiple_payload["scenario_status"] == "available"
    assert len(multiple_payload["applied_restriction_edge_ids"]) == 2
    assert multiple_payload["scenario"]["route_id"] == multiple_payload["baseline"][
        "route_id"
    ]
    assert lane_restriction.status_code == 200
    lane_payload = lane_restriction.json()
    assert lane_payload["evidence_level"] == "modeled_uncalibrated"
    assert lane_payload["scenario_status"] == "available"
    assert lane_payload["scenario"]["travel_time_seconds"] > lane_payload["baseline"][
        "travel_time_seconds"
    ]
    assert speed_restriction.status_code == 200
    speed_payload = speed_restriction.json()
    assert speed_payload["evidence_level"] == "modeled_uncalibrated"
    assert speed_payload["scenario"]["travel_time_seconds"] > speed_payload[
        "baseline"
    ]["travel_time_seconds"]
    assert ineffective_lane_restriction.status_code == 409
    assert ineffective_lane_restriction.json()["detail"]["code"] == (
        "closure_edge_mismatch"
    )
    assert scheduled_active.status_code == 200
    assert scheduled_active.json()["scenario_status"] == "no_route"
    assert scheduled_active.json()["inactive_restriction_edge_ids"] == []
    assert scheduled_at_end_boundary.status_code == 200
    boundary_payload = scheduled_at_end_boundary.json()
    assert boundary_payload["scenario_status"] == "inactive"
    assert boundary_payload["applied_restriction_edge_ids"] == []
    assert boundary_payload["inactive_restriction_edge_ids"] == [
        directions[("1", "2")]["edge_id"]
    ]


def test_directed_graph_reports_no_route_against_one_way_edges(tmp_path: Path) -> None:
    graph = _synthetic_graph()
    graph.remove_edge(2, 1, 0)
    graph.remove_edge(3, 2, 0)
    normalize_graph(graph, "one-way-fixture-v1")
    region = _region()
    report = validate_graph(graph, region, "one-way-fixture-v1")
    output_dir = tmp_path / "graphs"
    write_graph_artifacts(
        graph,
        region,
        "one-way-fixture-v1",
        output_dir,
        report,
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=output_dir / "portland-vancouver.graphml",
        graph_manifest_path=output_dir / "graph-manifest.json",
    )

    with TestClient(create_app(settings)) as client:
        forward = client.post(
            "/api/routes/compare",
            json={
                "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
                "destination": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
            },
        )
        reverse = client.post(
            "/api/routes/compare",
            json={
                "origin": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
                "destination": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
            },
        )

    assert forward.status_code == 200
    assert reverse.status_code == 404
    assert reverse.json()["detail"]["code"] == "no_legal_route"


def test_alternatives_are_distinct_and_google_handoff_uses_waypoints(
    tmp_path: Path,
) -> None:
    graph = normalize_graph(_alternative_graph(), "alternatives-fixture-v1")
    region = _region()
    report = validate_graph(graph, region, "alternatives-fixture-v1")
    output_dir = tmp_path / "graphs"
    write_graph_artifacts(
        graph,
        region,
        "alternatives-fixture-v1",
        output_dir,
        report,
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=output_dir / "portland-vancouver.graphml",
        graph_manifest_path=output_dir / "graph-manifest.json",
    )
    request = {
        "origin": {"lat": 45.51, "lng": -122.68, "node_id": "1"},
        "destination": {"lat": 45.53, "lng": -122.66, "node_id": "3"},
    }

    with TestClient(create_app(settings)) as client:
        alternatives_response = client.post("/api/routes/alternatives", json=request)
        alternatives = alternatives_response.json()
        fastest = alternatives["routes"][0]["route"]
        google_response = client.post(
            "/api/routes/google-maps-url",
            json={
                "origin": request["origin"],
                "destination": request["destination"],
                "route": fastest,
            },
        )

    assert alternatives_response.status_code == 200
    assert [route["ranking"] for route in alternatives["routes"]] == [
        "fastest",
        "reliable",
        "balanced",
    ]
    assert len({route["route"]["route_id"] for route in alternatives["routes"]}) == 3
    assert all(
        route["overlap_with_fastest_percent"] < 85
        for route in alternatives["routes"][1:]
    )
    assert max(
        route["residential_distance_percent"] for route in alternatives["routes"]
    ) == 100

    assert google_response.status_code == 200
    google = google_response.json()
    assert google["url"].startswith("https://www.google.com/maps/dir/?api=1")
    assert "travelmode=driving" in google["url"]
    assert "waypoints=" in google["url"]
    assert 1 <= google["waypoint_count"] <= 3
    assert "recalculates independently" in google["warning"]


def test_old_scenario_edges_rematch_only_with_high_confidence(tmp_path: Path) -> None:
    graph = normalize_graph(_synthetic_graph(), "rematch-current-v2")
    region = _region()
    report = validate_graph(graph, region, "rematch-current-v2")
    output_dir = tmp_path / "graphs"
    write_graph_artifacts(
        graph,
        region,
        "rematch-current-v2",
        output_dir,
        report,
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=output_dir / "portland-vancouver.graphml",
        graph_manifest_path=output_dir / "graph-manifest.json",
    )

    with TestClient(create_app(settings)) as client:
        origin = client.post(
            "/api/roads/select", json={"lat": 45.51, "lng": -122.68}
        ).json()["location"]
        destination = client.post(
            "/api/roads/select", json={"lat": 45.53, "lng": -122.66}
        ).json()["location"]
        closure = client.post(
            "/api/roads/select-closure",
            json={"lat": 45.515, "lng": -122.675},
        ).json()
        selected = closure["directions"][0]
        current_edge_id = selected["edge"]["edge_id"]
        selected["edge"]["edge_id"] = "old-version-edge"
        closure["graph_version"] = "rematch-old-v1"
        closure["selected_edge_id"] = "old-version-edge"
        created = client.post(
            "/api/scenarios",
            json={
                "name": "Old graph scenario",
                "content": {
                    "schema_version": 1,
                    "graph_version": "rematch-old-v1",
                    "origin": origin,
                    "destination": destination,
                    "departure_time": "2026-08-01T08:00:00-07:00",
                    "closures": [
                        {
                            "selection": closure,
                            "selected_edge_ids": ["old-version-edge"],
                            "restriction": {"type": "full"},
                        }
                    ],
                    "selected_route_id": None,
                },
            },
        )

    assert created.status_code == 201
    payload = created.json()
    assert payload["graph_status"] == "rematched"
    assert payload["graph_version"] == "rematch-old-v1"
    assert payload["content"]["graph_version"] == "rematch-current-v2"
    assert payload["content"]["closures"][0]["selected_edge_ids"] == [
        current_edge_id
    ]
    assert payload["warnings"]
