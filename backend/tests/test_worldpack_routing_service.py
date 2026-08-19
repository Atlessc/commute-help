from dataclasses import dataclass

import networkx as nx
import numpy as np
import pytest

from backend.app.services.worldpack_routing_service import (
    WorldPackNoRouteError,
    WorldPackRoutingService,
)
from backend.app.services.worldpack_service import (
    WorldPackAppEdgeSample,
    WorldPackRoadSample,
)


@dataclass
class _FakeWorldPack:
    loaded: bool = True

    def __post_init__(self) -> None:
        self.sample_times_s = np.asarray(
            [0.0, 5.0, 10.0],
            dtype=np.float32,
        )
        self.sample_times_s.setflags(
            write=False
        )

    def snapshot_index_for_time(
        self,
        requested_time_s: float,
    ) -> int:
        index = (
            int(
                np.searchsorted(
                    self.sample_times_s,
                    requested_time_s,
                    side="right",
                )
            )
            - 1
        )

        if index < 0:
            raise RuntimeError(
                "before first snapshot"
            )

        return index

    def moss_roads_for_app_edge(
        self,
        app_edge_id: str,
    ) -> tuple[int, ...]:
        if app_edge_id in {
            "direct",
            "a",
            "changing",
        }:
            return (
                hash(app_edge_id)
                & 0xFFFF,
            )

        return ()

    def app_edge_sample(
        self,
        app_edge_id: str,
        requested_time_s: float,
    ) -> WorldPackAppEdgeSample:
        snapshot = float(
            self.sample_times_s[
                self.snapshot_index_for_time(
                    requested_time_s
                )
            ]
        )

        speeds = {
            "direct": {
                0.0: 1.0,
                5.0: 1.0,
                10.0: 1.0,
            },
            "a": {
                0.0: 10.0,
                5.0: 10.0,
                10.0: 10.0,
            },
            "changing": {
                0.0: 1.0,
                5.0: 20.0,
                10.0: 20.0,
            },
        }

        speed = speeds[
            app_edge_id
        ][snapshot]

        moss_id = (
            hash(app_edge_id)
            & 0xFFFF
        )

        road = WorldPackRoadSample(
            moss_road_id=moss_id,
            source_sumo_edge_id=(
                f"sumo-{app_edge_id}"
            ),
            snapshot_time_s=snapshot,
            road_length_m=100.0,
            free_flow_speed_mps=20.0,
            raw_mean_speed_mps=speed,
            effective_speed_mps=speed,
            vehicle_count=1,
            waiting_vehicle_count=0,
            speed_source="observed",
        )

        return WorldPackAppEdgeSample(
            app_edge_id=app_edge_id,
            requested_time_s=float(
                requested_time_s
            ),
            snapshot_time_s=snapshot,
            mapped=True,
            moss_road_ids=(
                moss_id,
            ),
            road_samples=(
                road,
            ),
            equivalent_speed_mps=speed,
        )


def _edge(
    graph: nx.MultiDiGraph,
    u: str,
    v: str,
    edge_id: str,
    length_m: float,
) -> None:
    graph.add_edge(
        u,
        v,
        key=0,
        edge_id=edge_id,
        length_m=length_m,
    )


def test_advances_clock_while_inside_edge() -> None:
    graph = nx.MultiDiGraph()

    _edge(
        graph,
        "o",
        "x",
        "a",
        40.0,
    )

    _edge(
        graph,
        "x",
        "d",
        "changing",
        100.0,
    )

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    route = service.route(
        graph,
        "o",
        "d",
        2.0,
    )

    first = route.traversals[0]
    second = route.traversals[1]

    assert first.entry_time_s == pytest.approx(
        2.0
    )

    assert first.exit_time_s == pytest.approx(
        6.0
    )

    # First edge crosses t=5 even though speed is unchanged.
    assert first.snapshot_times_s == (
        0.0,
        5.0,
    )

    # Second edge begins at t=6, therefore starts with t=5.
    assert second.entry_time_s == pytest.approx(
        6.0
    )

    assert second.snapshot_time_s == pytest.approx(
        5.0
    )


def test_speed_change_applies_while_vehicle_is_on_edge() -> None:
    graph = nx.MultiDiGraph()

    _edge(
        graph,
        "o",
        "d",
        "changing",
        100.0,
    )

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    route = service.route(
        graph,
        "o",
        "d",
        4.0,
    )

    traversal = route.traversals[0]

    # t=4..5: 1 second at 1 m/s = 1 meter.
    # Remaining 99 m at 20 m/s = 4.95 seconds.
    assert traversal.travel_time_s == pytest.approx(
        5.95
    )

    assert traversal.snapshot_times_s == (
        0.0,
        5.0,
    )


def test_improving_snapshot_preserves_fifo() -> None:
    graph = nx.MultiDiGraph()

    _edge(
        graph,
        "o",
        "d",
        "changing",
        100.0,
    )

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    early = service.route(
        graph,
        "o",
        "d",
        4.9,
    )

    later = service.route(
        graph,
        "o",
        "d",
        5.0,
    )

    # The vehicle that entered first must not arrive later.
    assert (
        early.arrival_time_s
        <= later.arrival_time_s
        + 1e-9
    )


def test_route_choice_changes_with_departure_time() -> None:
    graph = nx.MultiDiGraph()

    # Constant route = 9.5 seconds.
    _edge(
        graph,
        "o",
        "d",
        "direct",
        9.5,
    )

    _edge(
        graph,
        "o",
        "x",
        "a",
        40.0,
    )

    _edge(
        graph,
        "x",
        "d",
        "changing",
        100.0,
    )

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    early = service.route(
        graph,
        "o",
        "d",
        0.0,
    )

    later = service.route(
        graph,
        "o",
        "d",
        5.0,
    )

    # t=0 alternative:
    # 4 sec first edge, then
    # 1 sec at 1 m/s + 99 m / 20 m/s
    # = 9.95 seconds total.
    assert early.edge_ids == (
        "direct",
    )

    # t=5 alternative = 4 + 5 = 9 seconds.
    assert later.edge_ids == (
        "a",
        "changing",
    )


def test_unmapped_edges_are_not_silently_used() -> None:
    graph = nx.MultiDiGraph()

    _edge(
        graph,
        "o",
        "d",
        "unmapped",
        1.0,
    )

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    with pytest.raises(
        WorldPackNoRouteError
    ):
        service.route(
            graph,
            "o",
            "d",
            0.0,
        )


def test_origin_equals_destination_is_zero_cost() -> None:
    graph = nx.MultiDiGraph()
    graph.add_node("o")

    service = WorldPackRoutingService(
        _FakeWorldPack()
    )

    route = service.route(
        graph,
        "o",
        "o",
        5.0,
    )

    assert route.travel_time_s == 0
    assert route.distance_m == 0
    assert route.edge_ids == ()
