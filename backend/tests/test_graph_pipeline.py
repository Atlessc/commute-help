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
    with TestClient(create_app(settings)) as client:
        status_response = client.get("/api/status")
        manifest_response = client.get("/api/graph/manifest")

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
