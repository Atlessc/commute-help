"""Curated closure preset safety and scope tests."""

from pathlib import Path
from types import SimpleNamespace

import networkx as nx
from fastapi.testclient import TestClient

from backend.app.core.settings import Settings
from backend.app.main import create_app
from backend.app.schemas.closure_presets import ClosurePresetCatalog
from backend.app.services.closure_preset_service import ClosurePresetService
from backend.app.services.graph_service import GraphService

CATALOG_PATH = Path("data/presets/closure-presets.json")


def _catalog() -> ClosurePresetCatalog:
    return ClosurePresetCatalog.model_validate_json(
        CATALOG_PATH.read_text(encoding="utf-8")
    )


def _matching_graph_service() -> GraphService:
    preset = _catalog().presets[0]
    service = GraphService(Path("unused.graphml"), Path("unused-manifest.json"))
    graph = nx.MultiDiGraph()
    for section in preset.sections:
        for direction in section.selection.directions:
            edge = direction.edge
            graph.add_edge(
                int(edge.u),
                int(edge.v),
                key=edge.key,
                edge_id=edge.edge_id,
                geometry_fingerprint=edge.geometry_fingerprint,
            )
    service.graph = graph
    service.node_lookup = {str(node): node for node in graph.nodes}
    service.manifest = SimpleNamespace(graph_version=preset.graph_version)  # type: ignore[assignment]
    return service


def test_rose_quarter_preset_has_only_the_marked_directed_impacts() -> None:
    preset = _catalog().presets[0]

    assert preset.id == "i5-rose-quarter-southbound-fall-2026"
    assert len(preset.sections) == 11
    assert sum(section.restriction.type == "full" for section in preset.sections) == 8
    assert sum(section.restriction.type == "lane" for section in preset.sections) == 3
    assert all(
        direction.direction_label != "Northbound"
        for section in preset.sections
        for direction in section.selection.directions
        if direction.edge.edge_id in section.selected_edge_ids
    )
    assert not preset.timing.exact_end_confirmed
    assert any("I-5 northbound" in notice for notice in preset.notices)
    assert any("detour to I-405" in notice for notice in preset.notices)


def test_preset_is_current_only_when_every_directed_edge_identity_matches() -> None:
    graph_service = _matching_graph_service()
    service = ClosurePresetService(CATALOG_PATH, graph_service)

    current = service.list()[0]
    assert current.graph_status == "current"
    assert current.warnings == []

    first_section = current.sections[0]
    first_edge = first_section.selection.directions[0].edge
    node_u = graph_service.node_lookup[first_edge.u]
    node_v = graph_service.node_lookup[first_edge.v]
    assert graph_service.graph is not None
    graph_service.graph[node_u][node_v][first_edge.key][
        "geometry_fingerprint"
    ] = "changed"

    reviewed = service.list()[0]
    assert reviewed.graph_status == "review_required"
    assert "directed edge identity" in reviewed.warnings[0]


def test_closure_preset_api_is_read_only_and_requires_graph_review(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "app.db",
        graph_path=tmp_path / "missing.graphml",
        graph_manifest_path=tmp_path / "missing-manifest.json",
        closure_preset_path=CATALOG_PATH,
        traffic_path=tmp_path / "traffic",
    )

    with TestClient(create_app(settings)) as client:
        list_response = client.get("/api/closure-presets")
        missing_response = client.get("/api/closure-presets/not-a-preset")

    assert list_response.status_code == 200
    preset = list_response.json()["presets"][0]
    assert preset["graph_status"] == "review_required"
    assert len(preset["sections"]) == 11
    assert missing_response.status_code == 404
    assert missing_response.json()["detail"]["code"] == "closure_preset_not_found"
