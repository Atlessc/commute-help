from __future__ import annotations

import networkx as nx
import pandas as pd
import pytest

from backend.app.services.background_seed_service import (
    BackgroundSeedConfig,
)
from backend.app.services.background_seed_v2_service import (
    MOVEMENT_GATEWAY_GATEWAY,
    MOVEMENT_GATEWAY_INTERNAL,
    MOVEMENT_INTERNAL_GATEWAY,
    MOVEMENT_INTERNAL_INTERNAL,
    BackgroundSeedV2Error,
    build_gateway_aware_candidate_paths,
    movement_class_counts,
    prepare_gateway_zones,
)


def _graph() -> nx.DiGraph:
    graph = nx.DiGraph()

    def edge(
        u: str,
        v: str,
        edge_id: str,
        cost: float = 10.0,
    ) -> None:
        graph.add_edge(
            u,
            v,
            edge_id=edge_id,
            seed_cost=cost,
        )

    # North gateway enters at N1.
    edge("N1", "A", "n1-a")
    edge("A", "N1", "a-n1")

    # Internal network.
    edge("A", "B", "a-b")
    edge("B", "A", "b-a")

    # South gateway connects at S1.
    edge("B", "S1", "b-s1")
    edge("S1", "B", "s1-b")

    return graph


def _internal_zones() -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "zone_id": "internal-a",
                "node_id": "A",
                "x": 0.0,
                "y": 5000.0,
                "activity": 100.0,
            },
            {
                "zone_id": "internal-b",
                "node_id": "B",
                "x": 0.0,
                "y": -5000.0,
                "activity": 100.0,
            },
        ]
    )


def _gateway_zones() -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "gateway_id": "north-i-5",
                "x": 0.0,
                "y": 10000.0,
                "activity": 200.0,
                "inbound_edge_id": "north-in",
                "inbound_exterior_node_id": "NOUT",
                "inbound_interior_node_id": "N1",
                "inbound_cost_seconds": 5.0,
                "outbound_edge_id": "north-out",
                "outbound_interior_node_id": "N1",
                "outbound_exterior_node_id": "NOUT2",
                "outbound_cost_seconds": 5.0,
            },
            {
                "gateway_id": "south-i-5",
                "x": 0.0,
                "y": -10000.0,
                "activity": 200.0,
                "inbound_edge_id": "south-in",
                "inbound_exterior_node_id": "SOUT",
                "inbound_interior_node_id": "S1",
                "inbound_cost_seconds": 5.0,
                "outbound_edge_id": "south-out",
                "outbound_interior_node_id": "S1",
                "outbound_exterior_node_id": "SOUT2",
                "outbound_cost_seconds": 5.0,
            },
        ]
    )


def _config() -> BackgroundSeedConfig:
    return BackgroundSeedConfig(
        minimum_trip_distance_m=0,
        maximum_trip_distance_m=100000,
        maximum_od_pairs=100,
    )


def test_v2_builds_all_four_movement_classes() -> None:
    paths = build_gateway_aware_candidate_paths(
        _graph(),
        _internal_zones(),
        _gateway_zones(),
        _config(),
    )

    counts = movement_class_counts(paths)

    assert counts[MOVEMENT_INTERNAL_INTERNAL] == 2
    assert counts[MOVEMENT_GATEWAY_INTERNAL] == 4
    assert counts[MOVEMENT_INTERNAL_GATEWAY] == 4
    assert counts[MOVEMENT_GATEWAY_GATEWAY] == 2


def test_gateway_boundary_edges_are_explicitly_included() -> None:
    paths = build_gateway_aware_candidate_paths(
        _graph(),
        _internal_zones(),
        _gateway_zones(),
        _config(),
    )

    gateway_to_internal = next(
        path
        for path in paths
        if (
            path.movement_class
            == MOVEMENT_GATEWAY_INTERNAL
            and path.origin_zone == "north-i-5"
            and path.destination_zone == "internal-b"
        )
    )

    assert gateway_to_internal.edge_ids[0] == "north-in"
    assert gateway_to_internal.node_ids[0] == "NOUT"
    assert gateway_to_internal.node_ids[1] == "N1"

    internal_to_gateway = next(
        path
        for path in paths
        if (
            path.movement_class
            == MOVEMENT_INTERNAL_GATEWAY
            and path.origin_zone == "internal-a"
            and path.destination_zone == "south-i-5"
        )
    )

    assert internal_to_gateway.edge_ids[-1] == "south-out"
    assert internal_to_gateway.node_ids[-1] == "SOUT2"

    through_trip = next(
        path
        for path in paths
        if (
            path.movement_class
            == MOVEMENT_GATEWAY_GATEWAY
            and path.origin_zone == "north-i-5"
            and path.destination_zone == "south-i-5"
        )
    )

    assert through_trip.edge_ids[0] == "north-in"
    assert through_trip.edge_ids[-1] == "south-out"


def test_gateway_paths_have_consistent_node_edge_counts() -> None:
    paths = build_gateway_aware_candidate_paths(
        _graph(),
        _internal_zones(),
        _gateway_zones(),
        _config(),
    )

    assert all(
        len(path.node_ids)
        == len(path.edge_ids) + 1
        for path in paths
    )



def _raw_gateway_inventory() -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "gateway_id": "north-i-5",
                "side": "north",
                "ref": "I 5",
                "movement": "inbound",
                "edge_id": "north-in",
                "interior_node_id": "N1",
                "exterior_node_id": "NOUT",
                "crossing_lon": -122.70,
                "crossing_lat": 45.85,
            },
            {
                "gateway_id": "north-i-5",
                "side": "north",
                "ref": "I 5",
                "movement": "outbound",
                "edge_id": "north-out",
                "interior_node_id": "N2",
                "exterior_node_id": "NOUT2",
                "crossing_lon": -122.70,
                "crossing_lat": 45.85,
            },
        ]
    )


def _gateway_priors() -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "edge_id": "north-in",
                "u": "NOUT",
                "v": "N1",
                "road_class": "motorway",
                "estimated_capacity_vph": 6000.0,
                "free_flow_seconds": 5.0,
            },
            {
                "edge_id": "north-out",
                "u": "N2",
                "v": "NOUT2",
                "road_class": "motorway",
                "estimated_capacity_vph": 6000.0,
                "free_flow_seconds": 6.0,
            },
        ]
    )


def test_prepare_gateway_zones_uses_graph_edges_and_projected_coordinates() -> None:
    zones = prepare_gateway_zones(
        _raw_gateway_inventory(),
        _gateway_priors(),
    )

    assert len(zones) == 1

    row = zones.iloc[0]

    assert row["gateway_id"] == "north-i-5"
    assert row["inbound_edge_id"] == "north-in"
    assert row["outbound_edge_id"] == "north-out"
    assert row["inbound_interior_node_id"] == "N1"
    assert row["outbound_interior_node_id"] == "N2"

    # Coordinates must now be projected meters, not lon/lat.
    assert row["x"] > 100000
    assert row["y"] > 1000000

    # Motorway route factor is 1.0.
    assert row["inbound_cost_seconds"] == 5.0
    assert row["outbound_cost_seconds"] == 6.0

    assert row["activity"] > 0


def test_prepare_gateway_zones_rejects_direction_mismatch() -> None:
    priors = _gateway_priors()
    priors.loc[
        priors["edge_id"] == "north-in",
        "u",
    ] = "WRONG"

    with pytest.raises(
        BackgroundSeedV2Error,
        match="edge direction does not match",
    ):
        prepare_gateway_zones(
            _raw_gateway_inventory(),
            priors,
        )



def test_gateway_and_internal_zone_may_share_interior_node() -> None:
    internal_zones = pd.concat(
        [
            _internal_zones(),
            pd.DataFrame.from_records(
                [
                    {
                        "zone_id": "boundary-zone",
                        "node_id": "N1",
                        "x": 0.0,
                        "y": 9000.0,
                        "activity": 100.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    paths = build_gateway_aware_candidate_paths(
        _graph(),
        internal_zones,
        _gateway_zones(),
        _config(),
    )

    inbound_only = next(
        path
        for path in paths
        if (
            path.movement_class
            == MOVEMENT_GATEWAY_INTERNAL
            and path.origin_zone == "north-i-5"
            and path.destination_zone == "boundary-zone"
        )
    )

    assert inbound_only.edge_ids == ("north-in",)
    assert inbound_only.node_ids == ("NOUT", "N1")
    assert inbound_only.route_free_flow_seconds == 5.0

    outbound_only = next(
        path
        for path in paths
        if (
            path.movement_class
            == MOVEMENT_INTERNAL_GATEWAY
            and path.origin_zone == "boundary-zone"
            and path.destination_zone == "north-i-5"
        )
    )

    assert outbound_only.edge_ids == ("north-out",)
    assert outbound_only.node_ids == ("N1", "NOUT2")
    assert outbound_only.route_free_flow_seconds == 5.0
