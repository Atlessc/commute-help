from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.services.gateway_inventory_service import (
    GatewayInventoryError,
    load_gateway_inventory,
)


def _gateway(
    gateway_id: str,
    side: str,
    ref: str,
    movement: str,
    edge_id: str,
) -> dict[str, object]:
    return {
        "gateway_id": gateway_id,
        "side": side,
        "ref": ref,
        "movement": movement,
        "edge_id": edge_id,
        "interior_node_id": f"{edge_id}-inside",
        "exterior_node_id": f"{edge_id}-outside",
        "crossing_lon": -122.5,
        "crossing_lat": 45.5,
        "road_class": "motorway",
        "road_name": "Test Highway",
    }


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "gateway_inventory_version": (
            "portland-vancouver-regional-gateways-v1"
        ),
        "graph_version": "graph-v1",
        "graph_edges_sha256": "edges-sha",
        "region": {
            "id": "region-v1",
            "name": "Test Region",
            "bounds": [-123.0, 45.0, -122.0, 46.0],
        },
        "corridor_count": 2,
        "directed_gateway_edge_count": 4,
        "gateways": [
            _gateway(
                "north-i-5",
                "north",
                "I 5",
                "inbound",
                "edge-1",
            ),
            _gateway(
                "north-i-5",
                "north",
                "I 5",
                "outbound",
                "edge-2",
            ),
            _gateway(
                "east-i-84",
                "east",
                "I 84",
                "inbound",
                "edge-3",
            ),
            _gateway(
                "east-i-84",
                "east",
                "I 84",
                "outbound",
                "edge-4",
            ),
        ],
    }


def _write_json(
    path: Path,
    payload: dict[str, object],
) -> None:
    path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _manifest() -> dict[str, object]:
    return {
        "graph_version": "graph-v1",
        "artifacts": {
            "edges_parquet": {
                "sha256": "edges-sha",
            }
        },
    }


def test_load_gateway_inventory_accepts_bidirectional_gateways(
    tmp_path: Path,
) -> None:
    inventory_path = tmp_path / "gateways.json"
    manifest_path = tmp_path / "graph-manifest.json"

    _write_json(inventory_path, _payload())
    _write_json(manifest_path, _manifest())

    gateways, metadata = load_gateway_inventory(
        inventory_path,
        manifest_path,
    )

    assert len(gateways) == 4
    assert gateways["gateway_id"].nunique() == 2
    assert set(gateways["movement"]) == {
        "inbound",
        "outbound",
    }
    assert metadata["graph_version"] == "graph-v1"
    assert metadata["corridor_count"] == 2
    assert metadata["directed_gateway_edge_count"] == 4


def test_graph_version_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    inventory = _payload()
    inventory["graph_version"] = "wrong-graph"

    inventory_path = tmp_path / "gateways.json"
    manifest_path = tmp_path / "graph-manifest.json"

    _write_json(inventory_path, inventory)
    _write_json(manifest_path, _manifest())

    with pytest.raises(
        GatewayInventoryError,
        match="does not match active graph version",
    ):
        load_gateway_inventory(
            inventory_path,
            manifest_path,
        )


def test_gateway_missing_direction_is_rejected(
    tmp_path: Path,
) -> None:
    inventory = _payload()
    gateways = inventory["gateways"]

    assert isinstance(gateways, list)

    inventory["gateways"] = gateways[:-1]
    inventory["directed_gateway_edge_count"] = 3

    inventory_path = tmp_path / "gateways.json"
    manifest_path = tmp_path / "graph-manifest.json"

    _write_json(inventory_path, inventory)
    _write_json(manifest_path, _manifest())

    with pytest.raises(
        GatewayInventoryError,
        match="one inbound and one outbound edge",
    ):
        load_gateway_inventory(
            inventory_path,
            manifest_path,
        )


def test_duplicate_gateway_edge_id_is_rejected(
    tmp_path: Path,
) -> None:
    inventory = _payload()
    gateways = inventory["gateways"]

    assert isinstance(gateways, list)
    assert isinstance(gateways[1], dict)

    gateways[1]["edge_id"] = "edge-1"

    inventory_path = tmp_path / "gateways.json"
    manifest_path = tmp_path / "graph-manifest.json"

    _write_json(inventory_path, inventory)
    _write_json(manifest_path, _manifest())

    with pytest.raises(
        GatewayInventoryError,
        match="edge_id values must be unique",
    ):
        load_gateway_inventory(
            inventory_path,
            manifest_path,
        )
