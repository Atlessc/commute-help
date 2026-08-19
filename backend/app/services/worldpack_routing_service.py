"""Passive time-dependent routing through an immutable WorldPack."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from itertools import count
from typing import Any

import networkx as nx

from backend.app.services.worldpack_service import (
    WorldPackAppEdgeSample,
    WorldPackService,
)


class WorldPackRouteError(RuntimeError):
    """A passive WorldPack route could not be calculated."""


class WorldPackNoRouteError(WorldPackRouteError):
    """No fully WorldPack-covered route exists."""


@dataclass(frozen=True)
class WorldPackTraversalSlice:
    """One portion of an edge traversed under one WorldPack snapshot."""

    snapshot_time_s: float
    start_time_s: float
    end_time_s: float
    distance_m: float
    equivalent_speed_mps: float
    moss_road_ids: tuple[int, ...]
    speed_sources: tuple[str, ...]


@dataclass(frozen=True)
class WorldPackEdgeTraversal:
    """Traversal of one canonical Commute Help edge."""

    edge_id: str
    u: Any
    v: Any
    key: int
    entry_time_s: float
    exit_time_s: float
    travel_time_s: float
    length_m: float
    slices: tuple[WorldPackTraversalSlice, ...]

    @property
    def snapshot_time_s(self) -> float:
        return self.slices[0].snapshot_time_s

    @property
    def equivalent_speed_mps(self) -> float:
        return self.length_m / self.travel_time_s

    @property
    def moss_road_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    road_id
                    for slice_ in self.slices
                    for road_id in slice_.moss_road_ids
                }
            )
        )

    @property
    def speed_sources(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    source
                    for slice_ in self.slices
                    for source in slice_.speed_sources
                }
            )
        )

    @property
    def snapshot_times_s(self) -> tuple[float, ...]:
        return tuple(
            slice_.snapshot_time_s
            for slice_ in self.slices
        )


@dataclass(frozen=True)
class WorldPackRoute:
    """Developer-facing passive-droplet route."""

    origin: Any
    destination: Any
    departure_time_s: float
    arrival_time_s: float
    travel_time_s: float
    distance_m: float
    nodes: tuple[Any, ...]
    edge_ids: tuple[str, ...]
    traversals: tuple[WorldPackEdgeTraversal, ...]
    expanded_states: int


@dataclass(frozen=True)
class _Predecessor:
    previous_node: Any
    traversal: WorldPackEdgeTraversal


class WorldPackRoutingService:
    """Earliest-arrival routing through a precomputed traffic world.

    The selected vehicle never changes background traffic.

    While the vehicle remains on an edge, WorldPack speed changes are
    applied when their snapshots become causally active. In other words:

        enter edge at t=14
        use t=10 state until t=15
        use t=15 state from t=15 onward

    This avoids freezing a single entry-time speed across the entire edge.
    Vehicles already on the same edge receive the same subsequent speed
    evolution, preserving FIFO ordering and eliminating loop-as-wait
    behavior from the routing search.
    """

    def __init__(
        self,
        worldpack_service: WorldPackService,
    ) -> None:
        if not worldpack_service.loaded:
            raise WorldPackRouteError(
                "WorldPackService must be loaded before routing"
            )

        self.worldpack = worldpack_service

    def route(
        self,
        graph: nx.MultiDiGraph,
        origin: Any,
        destination: Any,
        departure_time_s: float,
    ) -> WorldPackRoute:
        """Find the earliest-arrival fully WorldPack-covered route."""

        if origin not in graph:
            raise WorldPackRouteError(
                f"Origin node is not in graph: {origin}"
            )

        if destination not in graph:
            raise WorldPackRouteError(
                f"Destination node is not in graph: {destination}"
            )

        if not math.isfinite(departure_time_s):
            raise WorldPackRouteError(
                "departure_time_s must be finite"
            )

        # Ensure a causal state exists for departure.
        self.worldpack.snapshot_index_for_time(
            departure_time_s
        )

        if origin == destination:
            return WorldPackRoute(
                origin=origin,
                destination=destination,
                departure_time_s=float(departure_time_s),
                arrival_time_s=float(departure_time_s),
                travel_time_s=0.0,
                distance_m=0.0,
                nodes=(origin,),
                edge_ids=(),
                traversals=(),
                expanded_states=0,
            )

        best_arrival: dict[Any, float] = {
            origin: float(departure_time_s)
        }

        predecessor: dict[
            Any,
            _Predecessor,
        ] = {}

        sequence = count()

        queue: list[
            tuple[
                float,
                int,
                Any,
            ]
        ] = [
            (
                float(departure_time_s),
                next(sequence),
                origin,
            )
        ]

        # One edge has identical state within one WorldPack snapshot.
        sample_cache: dict[
            tuple[str, int],
            WorldPackAppEdgeSample,
        ] = {}

        expanded_states = 0
        destination_reached = False

        while queue:
            (
                arrival_time,
                _,
                node,
            ) = heapq.heappop(queue)

            known = best_arrival.get(node)

            if (
                known is None
                or arrival_time > known + 1e-9
            ):
                continue

            expanded_states += 1

            if node == destination:
                destination_reached = True
                break

            for (
                _u,
                v,
                key,
                edge,
            ) in graph.out_edges(
                node,
                keys=True,
                data=True,
            ):
                edge_id_raw = edge.get("edge_id")

                if edge_id_raw is None:
                    continue

                edge_id = str(edge_id_raw)

                # v0 uses only exact accepted mappings.
                if not self.worldpack.moss_roads_for_app_edge(
                    edge_id
                ):
                    continue

                try:
                    length_m = float(
                        edge["length_m"]
                    )
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise WorldPackRouteError(
                        "Canonical graph edge is missing "
                        f"valid length_m: {edge_id}"
                    ) from error

                if (
                    not math.isfinite(length_m)
                    or length_m <= 0
                ):
                    raise WorldPackRouteError(
                        f"Invalid canonical edge length: {edge_id}"
                    )

                traversal = self._traverse_edge(
                    edge_id=edge_id,
                    u=node,
                    v=v,
                    key=int(key),
                    length_m=length_m,
                    entry_time_s=arrival_time,
                    sample_cache=sample_cache,
                )

                next_arrival = traversal.exit_time_s

                prior = best_arrival.get(v)

                if (
                    prior is not None
                    and next_arrival >= prior - 1e-9
                ):
                    continue

                best_arrival[v] = next_arrival

                predecessor[v] = _Predecessor(
                    previous_node=node,
                    traversal=traversal,
                )

                heapq.heappush(
                    queue,
                    (
                        next_arrival,
                        next(sequence),
                        v,
                    ),
                )

        if not destination_reached:
            raise WorldPackNoRouteError(
                "No fully WorldPack-covered directed route "
                "exists between the selected nodes"
            )

        reversed_traversals: list[
            WorldPackEdgeTraversal
        ] = []

        node = destination

        while node != origin:
            step = predecessor.get(node)

            if step is None:
                raise WorldPackRouteError(
                    "Route predecessor chain is incomplete"
                )

            reversed_traversals.append(
                step.traversal
            )

            node = step.previous_node

        traversals = tuple(
            reversed(
                reversed_traversals
            )
        )

        nodes: list[Any] = [origin]

        for traversal in traversals:
            nodes.append(
                traversal.v
            )

        # With FIFO edge traversal and strictly positive costs,
        # a settled earliest-arrival path should be simple.
        if len(nodes) != len(set(nodes)):
            raise WorldPackRouteError(
                "FIFO route unexpectedly contains a cycle"
            )

        arrival_time_s = best_arrival[
            destination
        ]

        distance_m = sum(
            traversal.length_m
            for traversal in traversals
        )

        return WorldPackRoute(
            origin=origin,
            destination=destination,
            departure_time_s=float(
                departure_time_s
            ),
            arrival_time_s=float(
                arrival_time_s
            ),
            travel_time_s=float(
                arrival_time_s
                - departure_time_s
            ),
            distance_m=float(distance_m),
            nodes=tuple(nodes),
            edge_ids=tuple(
                traversal.edge_id
                for traversal in traversals
            ),
            traversals=traversals,
            expanded_states=expanded_states,
        )

    def _traverse_edge(
        self,
        *,
        edge_id: str,
        u: Any,
        v: Any,
        key: int,
        length_m: float,
        entry_time_s: float,
        sample_cache: dict[
            tuple[str, int],
            WorldPackAppEdgeSample,
        ],
    ) -> WorldPackEdgeTraversal:
        """Integrate one edge across causally active WorldPack snapshots."""

        current_time = float(
            entry_time_s
        )

        remaining_m = float(
            length_m
        )

        slices: list[
            WorldPackTraversalSlice
        ] = []

        sample_times = (
            self.worldpack.sample_times_s
        )

        epsilon = 1e-9

        while remaining_m > epsilon:
            snapshot_index = (
                self.worldpack
                .snapshot_index_for_time(
                    current_time
                )
            )

            cache_key = (
                edge_id,
                snapshot_index,
            )

            sample = sample_cache.get(
                cache_key
            )

            if sample is None:
                snapshot_time = float(
                    sample_times[
                        snapshot_index
                    ]
                )

                sample = (
                    self.worldpack
                    .app_edge_sample(
                        edge_id,
                        snapshot_time,
                    )
                )

                if not sample.mapped:
                    raise WorldPackRouteError(
                        "Mapped edge became uncovered: "
                        f"{edge_id}"
                    )

                sample_cache[
                    cache_key
                ] = sample

            speed = (
                sample.equivalent_speed_mps
            )

            if (
                speed is None
                or not math.isfinite(speed)
                or speed <= 0
            ):
                raise WorldPackRouteError(
                    "WorldPack produced invalid speed "
                    f"for edge {edge_id}"
                )

            if sample.snapshot_time_s is None:
                raise WorldPackRouteError(
                    "Mapped WorldPack edge has no snapshot time"
                )

            if (
                snapshot_index + 1
                < len(sample_times)
            ):
                next_boundary = float(
                    sample_times[
                        snapshot_index + 1
                    ]
                )

                available_time = max(
                    0.0,
                    next_boundary
                    - current_time,
                )

                available_distance = (
                    speed
                    * available_time
                )
            else:
                next_boundary = None
                available_time = math.inf
                available_distance = math.inf

            speed_sources = tuple(
                sorted(
                    {
                        road.speed_source
                        for road
                        in sample.road_samples
                    }
                )
            )

            if remaining_m <= (
                available_distance
                + epsilon
            ):
                duration = (
                    remaining_m
                    / speed
                )

                end_time = (
                    current_time
                    + duration
                )

                distance = remaining_m

                remaining_m = 0.0

            else:
                if next_boundary is None:
                    raise WorldPackRouteError(
                        "Unexpected WorldPack horizon state"
                    )

                duration = available_time
                end_time = next_boundary
                distance = available_distance

                remaining_m -= (
                    available_distance
                )

            if distance > epsilon:
                slices.append(
                    WorldPackTraversalSlice(
                        snapshot_time_s=float(
                            sample.snapshot_time_s
                        ),
                        start_time_s=current_time,
                        end_time_s=end_time,
                        distance_m=distance,
                        equivalent_speed_mps=float(
                            speed
                        ),
                        moss_road_ids=(
                            sample.moss_road_ids
                        ),
                        speed_sources=(
                            speed_sources
                        ),
                    )
                )

            current_time = end_time

        if not slices:
            raise WorldPackRouteError(
                f"Edge traversal produced no slices: {edge_id}"
            )

        return WorldPackEdgeTraversal(
            edge_id=edge_id,
            u=u,
            v=v,
            key=key,
            entry_time_s=float(
                entry_time_s
            ),
            exit_time_s=float(
                current_time
            ),
            travel_time_s=float(
                current_time
                - entry_time_s
            ),
            length_m=float(
                length_m
            ),
            slices=tuple(
                slices
            ),
        )
