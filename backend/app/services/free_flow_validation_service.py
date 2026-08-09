"""Hard physical free-flow floors for benchmark and simulated trips."""

from __future__ import annotations

from typing import Literal

import networkx as nx

from backend.app.schemas.benchmarks import BenchmarkTripRecord, FreeFlowValidation
from backend.app.services.graph_service import GraphService


class FreeFlowValidationService:
    def __init__(
        self,
        graph_service: GraphService,
        *,
        relative_tolerance: float = 0.03,
        minimum_tolerance_seconds: float = 5.0,
    ) -> None:
        self.graph_service = graph_service
        self.relative_tolerance = relative_tolerance
        self.minimum_tolerance_seconds = minimum_tolerance_seconds

    def validate(
        self,
        benchmark: BenchmarkTripRecord,
        observed_or_simulated_seconds: float | None = None,
        *,
        value_kind: Literal["observed", "reported_no_traffic", "simulated"] = "observed",
    ) -> FreeFlowValidation:
        graph = self.graph_service.graph
        manifest = self.graph_service.require_manifest()
        if graph is None:
            raise ValueError("Routing graph is unavailable")
        if benchmark.origin_node is None or benchmark.destination_node is None:
            raise ValueError("Zone-only benchmarks cannot receive a network floor yet")
        origin = self.graph_service.node_lookup.get(benchmark.origin_node)
        destination = self.graph_service.node_lookup.get(benchmark.destination_node)
        if origin is None or destination is None:
            raise ValueError("Benchmark node does not exist in the active graph")
        path = nx.shortest_path(graph, origin, destination, weight="free_flow_seconds")
        floor_seconds = 0.0
        distance_m = 0.0
        for node_u, node_v in zip(path, path[1:], strict=False):
            candidates = graph.get_edge_data(node_u, node_v)
            if not candidates:
                raise ValueError("Free-flow path contains a missing directed edge")
            edge = min(candidates.values(), key=lambda item: float(item["free_flow_seconds"]))
            floor_seconds += float(edge["free_flow_seconds"])
            distance_m += float(edge["length_m"])
        tolerance = max(
            self.minimum_tolerance_seconds, floor_seconds * self.relative_tolerance
        )
        minimum_allowed = max(0.1, floor_seconds - tolerance)
        candidate_seconds = float(
            observed_or_simulated_seconds
            if observed_or_simulated_seconds is not None
            else benchmark.actual_travel_seconds
        )
        violation = max(0.0, minimum_allowed - candidate_seconds)
        return FreeFlowValidation(
            benchmark_id=benchmark.id,
            graph_version=manifest.graph_version,
            network_free_flow_seconds=round(floor_seconds, 3),
            tolerance_seconds=round(tolerance, 3),
            minimum_allowed_seconds=round(minimum_allowed, 3),
            observed_or_simulated_seconds=round(candidate_seconds, 3),
            value_kind=value_kind,
            valid=violation == 0,
            violation_seconds=round(violation, 3),
            path_edge_count=len(path) - 1,
            path_distance_m=round(distance_m, 3),
        )
