from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from backend.app.services.sumo.proxy_od_service import (
    ProxyOdError,
    _proxy_zones,
    _seed_zone_types,
)


def _nodes(tmp_path):
    path = tmp_path / "nodes.parquet"

    frame = gpd.GeoDataFrame(
        {
            "osmid": [
                1,
                2,
                3,
                4,
            ],
        },
        geometry=[
            Point(-122.70, 45.60),
            Point(-122.60, 45.55),
            Point(-122.70, 45.85),
            Point(-122.701, 45.851),
        ],
        crs="EPSG:4326",
    )

    frame.to_parquet(path, index=False)

    return path


def test_v2_gateway_zone_can_use_two_directional_nodes(
    tmp_path,
) -> None:
    seeds = pd.DataFrame.from_records(
        [
            {
                "pair_id": "gateway-entry",
                "origin_zone": "north-i-5",
                "destination_zone": "internal-a",
                "origin_node_id": "3",
                "destination_node_id": "1",
                "origin_zone_type": "gateway",
                "destination_zone_type": "internal",
                "movement_class": "gateway_internal",
            },
            {
                "pair_id": "gateway-exit",
                "origin_zone": "internal-a",
                "destination_zone": "north-i-5",
                "origin_node_id": "1",
                "destination_node_id": "4",
                "origin_zone_type": "internal",
                "destination_zone_type": "gateway",
                "movement_class": "internal_gateway",
            },
        ]
    )

    zones = _proxy_zones(
        seeds,
        _nodes(tmp_path),
    ).set_index("zone_id")

    assert set(zones.index) == {
        "internal-a",
        "north-i-5",
    }

    assert (
        zones.loc["internal-a", "zone_type"]
        == "internal"
    )
    assert (
        zones.loc["north-i-5", "zone_type"]
        == "gateway"
    )

    assert not zones.loc[
        "north-i-5",
        "geometry",
    ].is_empty


def test_v1_seed_defaults_to_internal_zones(
    tmp_path,
) -> None:
    seeds = pd.DataFrame.from_records(
        [
            {
                "pair_id": "old-v1-pair",
                "origin_zone": "internal-a",
                "destination_zone": "internal-b",
                "origin_node_id": "1",
                "destination_node_id": "2",
            }
        ]
    )

    origins, destinations, movement = (
        _seed_zone_types(seeds)
    )

    assert origins.tolist() == ["internal"]
    assert destinations.tolist() == ["internal"]
    assert movement.tolist() == [
        "internal_internal"
    ]

    zones = _proxy_zones(
        seeds,
        _nodes(tmp_path),
    )

    assert set(zones["zone_type"]) == {
        "internal"
    }


def test_movement_class_must_match_zone_types() -> None:
    seeds = pd.DataFrame.from_records(
        [
            {
                "pair_id": "bad-pair",
                "origin_zone": "north-i-5",
                "destination_zone": "internal-a",
                "origin_node_id": "3",
                "destination_node_id": "1",
                "origin_zone_type": "gateway",
                "destination_zone_type": "internal",
                "movement_class": "internal_internal",
            }
        ]
    )

    with pytest.raises(
        ProxyOdError,
        match="movement_class does not match",
    ):
        _seed_zone_types(seeds)
