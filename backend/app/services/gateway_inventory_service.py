"""Load and validate graph-versioned regional gateway inventories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

GATEWAY_INVENTORY_SCHEMA_VERSION = 1

VALID_SIDES = frozenset({"north", "south", "east", "west"})
VALID_MOVEMENTS = frozenset({"inbound", "outbound"})

REQUIRED_GATEWAY_COLUMNS = {
    "gateway_id",
    "side",
    "ref",
    "movement",
    "edge_id",
    "interior_node_id",
    "exterior_node_id",
    "crossing_lon",
    "crossing_lat",
    "road_class",
    "road_name",
}

TEXT_GATEWAY_COLUMNS = {
    "gateway_id",
    "side",
    "ref",
    "movement",
    "edge_id",
    "interior_node_id",
    "exterior_node_id",
    "road_class",
    "road_name",
}


class GatewayInventoryError(RuntimeError):
    """A regional gateway inventory is malformed or incompatible."""


def load_gateway_inventory(
    inventory_path: Path,
    graph_manifest_path: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load one gateway inventory and verify it against the active graph."""

    payload = _read_json(inventory_path)
    graph_manifest = _read_json(graph_manifest_path)

    if payload.get("schema_version") != GATEWAY_INVENTORY_SCHEMA_VERSION:
        raise GatewayInventoryError(
            "Gateway inventory must use schema_version "
            f"{GATEWAY_INVENTORY_SCHEMA_VERSION}."
        )

    inventory_version = _required_string(
        payload,
        "gateway_inventory_version",
    )
    inventory_graph_version = _required_string(
        payload,
        "graph_version",
    )
    inventory_edges_sha256 = _required_string(
        payload,
        "graph_edges_sha256",
    )

    graph_version = _required_string(
        graph_manifest,
        "graph_version",
    )

    if inventory_graph_version != graph_version:
        raise GatewayInventoryError(
            "Gateway inventory graph version "
            f"{inventory_graph_version} does not match "
            f"active graph version {graph_version}."
        )

    artifacts = graph_manifest.get("artifacts")

    if not isinstance(artifacts, dict):
        raise GatewayInventoryError(
            "Graph manifest is missing artifacts."
        )

    edges_artifact = artifacts.get("edges_parquet")

    if not isinstance(edges_artifact, dict):
        raise GatewayInventoryError(
            "Graph manifest is missing the edges_parquet artifact."
        )

    graph_edges_sha256 = _required_string(
        edges_artifact,
        "sha256",
    )

    if inventory_edges_sha256 != graph_edges_sha256:
        raise GatewayInventoryError(
            "Gateway inventory edge checksum does not match "
            "the active graph edges artifact."
        )

    gateway_rows = payload.get("gateways")

    if not isinstance(gateway_rows, list) or not gateway_rows:
        raise GatewayInventoryError(
            "Gateway inventory must contain a nonempty gateways list."
        )

    frame = pd.DataFrame.from_records(gateway_rows)

    missing = sorted(
        REQUIRED_GATEWAY_COLUMNS - set(frame.columns)
    )

    if missing:
        raise GatewayInventoryError(
            "Gateway inventory rows are missing fields: "
            + ", ".join(missing)
        )

    frame = frame[sorted(REQUIRED_GATEWAY_COLUMNS)].copy()

    for column in TEXT_GATEWAY_COLUMNS:
        values = frame[column].astype("string").str.strip()

        if values.isna().any() or values.eq("").any():
            raise GatewayInventoryError(
                f"Gateway field {column} contains empty values."
            )

        frame[column] = values.astype(str)

    for column in ("crossing_lon", "crossing_lat"):
        values = pd.to_numeric(
            frame[column],
            errors="coerce",
        )

        if values.isna().any():
            raise GatewayInventoryError(
                f"Gateway field {column} contains non-numeric values."
            )

        frame[column] = values.astype(float)

    if not frame["crossing_lon"].between(-180, 180).all():
        raise GatewayInventoryError(
            "Gateway crossing_lon values must be valid longitudes."
        )

    if not frame["crossing_lat"].between(-90, 90).all():
        raise GatewayInventoryError(
            "Gateway crossing_lat values must be valid latitudes."
        )

    invalid_sides = sorted(
        set(frame["side"]) - VALID_SIDES
    )

    if invalid_sides:
        raise GatewayInventoryError(
            "Gateway inventory contains invalid sides: "
            + ", ".join(invalid_sides)
        )

    invalid_movements = sorted(
        set(frame["movement"]) - VALID_MOVEMENTS
    )

    if invalid_movements:
        raise GatewayInventoryError(
            "Gateway inventory contains invalid movements: "
            + ", ".join(invalid_movements)
        )

    if frame["edge_id"].duplicated().any():
        raise GatewayInventoryError(
            "Gateway inventory edge_id values must be unique."
        )

    if frame.duplicated(
        ["gateway_id", "movement"]
    ).any():
        raise GatewayInventoryError(
            "Each gateway may contain only one edge per movement."
        )

    for gateway_id, group in frame.groupby(
        "gateway_id",
        sort=True,
    ):
        directions = set(group["movement"])

        if len(group) != 2 or directions != VALID_MOVEMENTS:
            raise GatewayInventoryError(
                f"Gateway {gateway_id} must contain exactly "
                "one inbound and one outbound edge."
            )

        if group["side"].nunique() != 1:
            raise GatewayInventoryError(
                f"Gateway {gateway_id} spans multiple boundary sides."
            )

        if group["ref"].nunique() != 1:
            raise GatewayInventoryError(
                f"Gateway {gateway_id} spans multiple route refs."
            )

    actual_corridor_count = int(
        frame["gateway_id"].nunique()
    )
    actual_directed_edge_count = len(frame)

    declared_corridor_count = _required_int(
        payload,
        "corridor_count",
    )
    declared_directed_edge_count = _required_int(
        payload,
        "directed_gateway_edge_count",
    )

    if declared_corridor_count != actual_corridor_count:
        raise GatewayInventoryError(
            "Gateway corridor_count does not match "
            f"the gateway rows: declared "
            f"{declared_corridor_count}, actual "
            f"{actual_corridor_count}."
        )

    if (
        declared_directed_edge_count
        != actual_directed_edge_count
    ):
        raise GatewayInventoryError(
            "Gateway directed_gateway_edge_count does not "
            f"match the gateway rows: declared "
            f"{declared_directed_edge_count}, actual "
            f"{actual_directed_edge_count}."
        )

    frame = frame.sort_values(
        [
            "side",
            "ref",
            "movement",
            "edge_id",
        ],
        ignore_index=True,
    )

    metadata = {
        "schema_version": GATEWAY_INVENTORY_SCHEMA_VERSION,
        "gateway_inventory_version": inventory_version,
        "graph_version": graph_version,
        "graph_edges_sha256": graph_edges_sha256,
        "corridor_count": actual_corridor_count,
        "directed_gateway_edge_count": (
            actual_directed_edge_count
        ),
    }

    return frame, metadata


def _required_string(
    payload: dict[str, Any],
    key: str,
) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value.strip():
        raise GatewayInventoryError(
            f"{key} must be a nonempty string."
        )

    return value.strip()


def _required_int(
    payload: dict[str, Any],
    key: str,
) -> int:
    value = payload.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayInventoryError(
            f"{key} must be an integer."
        )

    if value < 1:
        raise GatewayInventoryError(
            f"{key} must be positive."
        )

    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise GatewayInventoryError(
            f"Cannot read {path.name}: {error}"
        ) from error

    if not isinstance(value, dict):
        raise GatewayInventoryError(
            f"{path.name} must contain a JSON object."
        )

    return value
