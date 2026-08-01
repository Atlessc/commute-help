"""Bounded synthetic demand assignment with progress, cancellation, and caching."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import logging
import math
from threading import Event, Lock
from typing import Any, Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import networkx as nx
import numpy as np
from shapely.geometry import Point

from backend.app.db.database import DatabaseManager
from backend.app.schemas.diversion import (
    DiversionEdgeChange,
    DiversionJobResponse,
    DiversionRequest,
    DiversionResult,
)
from backend.app.schemas.routing import GeoJSONLineString, RouteCompareRequest
from backend.app.services.graph_service import GraphService, GraphUnavailableError
from backend.app.services.routing_service import (
    AppliedRestriction,
    InvalidClosureError,
    NoRouteError,
    RoutingService,
    SameLocationError,
)
from backend.app.services.spatial_service import SpatialService

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
MODEL_VERSION = "incremental-msa-bpr-v1"
BPR_ALPHA = 0.15
BPR_BETA = 4.0


class DiversionJobNotFoundError(LookupError):
    """The requested process-local job does not exist."""


@dataclass
class _Job:
    id: UUID
    status: str
    progress_percent: int
    message: str
    cached: bool
    created_at: datetime
    updated_at: datetime
    result: DiversionResult | None = None
    error: str | None = None
    cancel_event: Event = field(default_factory=Event)


@dataclass(frozen=True)
class _Assignment:
    flows: dict[str, float]
    assigned_vph: float
    unassigned_vph: float


class _Cancelled(RuntimeError):
    pass


class DiversionService:
    """Run one bounded assignment worker without mutating the shared graph."""

    def __init__(
        self,
        database: DatabaseManager,
        graph_service: GraphService,
        spatial_service: SpatialService,
        routing_service: RoutingService,
    ) -> None:
        self.database = database
        self.graph_service = graph_service
        self.spatial_service = spatial_service
        self.routing_service = routing_service
        self._jobs: dict[UUID, _Job] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="commute-help-diversion",
        )

    def start(self, payload: DiversionRequest) -> DiversionJobResponse:
        """Return a cached result immediately or enqueue a bounded calculation."""

        graph_version = self.graph_service.require_manifest().graph_version
        if payload.graph_version != graph_version:
            raise InvalidClosureError(
                "The diversion inputs belong to a different road graph version."
            )
        cache_key = self._cache_key(payload)
        cached = self._cached_result(cache_key)
        now = datetime.now(PACIFIC)
        if cached is not None:
            job = _Job(
                id=uuid4(),
                status="completed",
                progress_percent=100,
                message="Loaded a matching modeled run from the local cache.",
                cached=True,
                created_at=now,
                updated_at=now,
                result=cached,
            )
            with self._lock:
                self._jobs[job.id] = job
            return self._response(job)

        job = _Job(
            id=uuid4(),
            status="queued",
            progress_percent=0,
            message="Waiting for the local diversion worker.",
            cached=False,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run_job, job.id, payload, cache_key)
        return self._response(job)

    def get(self, job_id: UUID) -> DiversionJobResponse:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DiversionJobNotFoundError("That diversion job is no longer available.")
            return self._response(job)

    def cancel(self, job_id: UUID) -> DiversionJobResponse:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DiversionJobNotFoundError("That diversion job is no longer available.")
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
                job.message = "Cancellation requested; finishing the current path."
                job.updated_at = datetime.now(PACIFIC)
            return self._response(job)

    def shutdown(self) -> None:
        """Stop accepting work and signal unfinished jobs during app shutdown."""

        with self._lock:
            for job in self._jobs.values():
                if job.status in {"queued", "running"}:
                    job.cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_job(
        self,
        job_id: UUID,
        payload: DiversionRequest,
        cache_key: str,
    ) -> None:
        self._update(job_id, status="running", progress=1, message="Preparing synthetic demand pairs.")
        try:
            result = self._calculate(job_id, payload, cache_key)
            self._store_cache(cache_key, payload, result)
        except _Cancelled:
            self._update(
                job_id,
                status="cancelled",
                message="The modeled diversion run was cancelled.",
            )
        except (
            GraphUnavailableError,
            InvalidClosureError,
            SameLocationError,
            ValueError,
        ) as error:
            self._update(
                job_id,
                status="failed",
                message="The modeled diversion run could not be completed.",
                error=str(error),
            )
        except Exception:
            logger.exception("Diversion job %s failed", job_id)
            self._update(
                job_id,
                status="failed",
                message="The modeled diversion run failed safely.",
                error="An internal diversion-model error occurred.",
            )
        else:
            self._update(
                job_id,
                status="completed",
                progress=100,
                message="Modeled diversion and spillover are ready.",
                result=result,
            )

    def _calculate(
        self,
        job_id: UUID,
        payload: DiversionRequest,
        cache_key: str,
    ) -> DiversionResult:
        graph = self.graph_service.graph
        manifest = self.graph_service.manifest
        if graph is None or manifest is None or not self.graph_service.ready:
            raise GraphUnavailableError("Routing graph has not been loaded.")

        origin = self.spatial_service.resolve_node(
            payload.origin.lat, payload.origin.lng, payload.origin.node_id
        )
        destination = self.spatial_service.resolve_node(
            payload.destination.lat, payload.destination.lng, payload.destination.node_id
        )
        if origin == destination:
            raise SameLocationError("Choose two different trip areas.")
        routing_payload = RouteCompareRequest(
            origin=payload.origin,
            destination=payload.destination,
            graph_version=payload.graph_version,
            departure_time=payload.departure_time,
            closures=payload.closures,
        )
        restrictions, _inactive = self.routing_service._validate_restrictions(
            graph,
            routing_payload,
            manifest.graph_version,
            payload.departure_time,
        )
        if not restrictions:
            raise InvalidClosureError(
                "At least one selected road restriction must be active at departure."
            )

        pairs = self._demand_pairs(
            origin,
            destination,
            payload.demand_pair_count,
            payload.dispersion_radius_m,
            cache_key,
        )
        check_cancelled = self._cancel_checker(job_id)
        baseline = self._assign(
            graph=graph,
            pairs=pairs,
            demand_vph=payload.demand_vph,
            iterations=payload.iterations,
            restrictions={},
            progress=lambda completed, total: self._assignment_progress(
                job_id, "baseline", completed, total
            ),
            check_cancelled=check_cancelled,
        )
        scenario_graph = nx.subgraph_view(
            graph,
            filter_edge=lambda u, v, key: not _is_full_closure(
                graph.edges[u, v, key], restrictions
            ),
        )
        scenario = self._assign(
            graph=scenario_graph,
            pairs=pairs,
            demand_vph=payload.demand_vph,
            iterations=payload.iterations,
            restrictions=restrictions,
            progress=lambda completed, total: self._assignment_progress(
                job_id, "scenario", completed, total
            ),
            check_cancelled=check_cancelled,
        )
        check_cancelled()
        self._update(job_id, progress=97, message="Preparing the spillover layer.")
        (
            edge_changes,
            changed_count,
            residential_increase,
            max_increase,
            max_decrease,
        ) = self._edge_changes(
            baseline.flows,
            scenario.flows,
            restrictions,
            payload.max_result_edges,
        )
        return DiversionResult(
            graph_version=manifest.graph_version,
            model_version=MODEL_VERSION,
            input_hash=cache_key,
            demand_vph=payload.demand_vph,
            demand_pair_count=len(pairs),
            iterations=payload.iterations,
            dispersion_radius_m=payload.dispersion_radius_m,
            assigned_demand_vph=scenario.assigned_vph,
            unassigned_demand_vph=scenario.unassigned_vph,
            changed_edge_count=changed_count,
            max_increase_vph=max_increase,
            max_decrease_vph=max_decrease,
            residential_increase_vph=max(0.0, residential_increase),
            edge_changes=edge_changes,
            assumptions=[
                "This is a synthetic relative-diversion model, not observed or live traffic.",
                "Demand pairs are deterministically dispersed around the selected trip endpoints.",
                "Routes use directed OpenStreetMap access, class penalties, and estimated hourly capacities.",
                "Congestion costs use the uncalibrated BPR formula with alpha 0.15 and beta 4.0.",
                "Incremental all-or-nothing assignments are averaged with the method of successive averages.",
                "Only the selected synthetic demand is modeled; background regional traffic and signal timing are omitted.",
            ],
        )

    def _demand_pairs(
        self,
        origin: Any,
        destination: Any,
        count: int,
        radius_m: int,
        cache_key: str,
    ) -> list[tuple[Any, Any]]:
        nodes = self.graph_service.projected_nodes
        graph = self.graph_service.graph
        if nodes is None or graph is None:
            raise GraphUnavailableError("Projected graph nodes are unavailable.")

        def nearby(center: Any) -> list[Any]:
            if radius_m == 0:
                return [center]
            point = nodes.loc[center].geometry
            positions = nodes.sindex.query(
                Point(point.x, point.y).buffer(radius_m),
                predicate="intersects",
            )
            candidates = [nodes.index[int(position)] for position in positions]
            return candidates or [center]

        starts = nearby(origin)
        ends = nearby(destination)
        seed = int.from_bytes(bytes.fromhex(cache_key[:16]), "big")
        generator = np.random.default_rng(seed)
        pairs: list[tuple[Any, Any]] = [(origin, destination)]
        while len(pairs) < count:
            start = starts[int(generator.integers(0, len(starts)))]
            end = ends[int(generator.integers(0, len(ends)))]
            if start != end:
                pairs.append((start, end))
        return pairs

    def _assign(
        self,
        *,
        graph: nx.MultiDiGraph,
        pairs: list[tuple[Any, Any]],
        demand_vph: int,
        iterations: int,
        restrictions: dict[str, AppliedRestriction],
        progress: Callable[[int, int], None],
        check_cancelled: Callable[[], None],
    ) -> _Assignment:
        flows: dict[str, float] = {}
        per_pair_vph = demand_vph / len(pairs)
        total_steps = iterations * len(pairs)
        completed_steps = 0
        final_unassigned_pairs = 0
        for iteration in range(1, iterations + 1):
            auxiliary: dict[str, float] = defaultdict(float)
            unassigned_pairs = 0
            weight = self._congested_weight(flows, restrictions)
            for origin, destination in pairs:
                check_cancelled()
                try:
                    path = self.routing_service.find_path(
                        graph, origin, destination, weight=weight
                    )
                except NoRouteError:
                    unassigned_pairs += 1
                else:
                    for u, v in zip(path, path[1:]):
                        edge = self._best_assignment_edge(
                            graph, u, v, flows, restrictions
                        )
                        auxiliary[str(edge["edge_id"])] += per_pair_vph
                completed_steps += 1
                progress(completed_steps, total_steps)
            step_size = 1 / iteration
            for edge_id in set(flows) | set(auxiliary):
                flows[edge_id] = flows.get(edge_id, 0.0) + step_size * (
                    auxiliary.get(edge_id, 0.0) - flows.get(edge_id, 0.0)
                )
            final_unassigned_pairs = unassigned_pairs
        unassigned_vph = final_unassigned_pairs * per_pair_vph
        return _Assignment(
            flows=flows,
            assigned_vph=max(0.0, demand_vph - unassigned_vph),
            unassigned_vph=unassigned_vph,
        )

    @staticmethod
    def _congested_weight(
        flows: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
    ) -> Callable[[Any, Any, dict[Any, dict[str, Any]]], float]:
        def weight(
            _u: Any,
            _v: Any,
            candidates: dict[Any, dict[str, Any]],
        ) -> float:
            return min(
                _congested_cost(
                    edge,
                    flows.get(str(edge["edge_id"]), 0.0),
                    restrictions.get(str(edge["edge_id"])),
                )
                for edge in candidates.values()
            )

        return weight

    @staticmethod
    def _best_assignment_edge(
        graph: nx.MultiDiGraph,
        u: Any,
        v: Any,
        flows: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
    ) -> dict[str, Any]:
        candidates = graph.get_edge_data(u, v)
        if not candidates:
            raise NoRouteError()
        return min(
            candidates.values(),
            key=lambda edge: _congested_cost(
                edge,
                flows.get(str(edge["edge_id"]), 0.0),
                restrictions.get(str(edge["edge_id"])),
            ),
        )

    def _edge_changes(
        self,
        baseline: dict[str, float],
        scenario: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
        limit: int,
    ) -> tuple[list[DiversionEdgeChange], int, float, float, float]:
        edges = self.graph_service.edges
        graph = self.graph_service.graph
        if edges is None or graph is None:
            raise GraphUnavailableError("Graph edge details are unavailable.")
        records: list[DiversionEdgeChange] = []
        for edge_id in set(baseline) | set(scenario):
            change = scenario.get(edge_id, 0.0) - baseline.get(edge_id, 0.0)
            if abs(change) < 0.5:
                continue
            position = self.graph_service.edge_id_positions.get(edge_id)
            if position is None:
                continue
            u, v, key = edges.index[position]
            edge = graph.edges[u, v, key]
            baseline_vph = baseline.get(edge_id, 0.0)
            scenario_vph = scenario.get(edge_id, 0.0)
            capacity = _effective_capacity(
                edge,
                restrictions.get(edge_id),
            )
            geometry = edge["geometry"]
            records.append(
                DiversionEdgeChange(
                    edge_id=edge_id,
                    u=str(u),
                    v=str(v),
                    key=int(key),
                    road_name=str(edge.get("road_name", "Unnamed road")),
                    road_class=str(edge.get("road_class", "unclassified")),
                    baseline_vph=round(baseline_vph, 1),
                    scenario_vph=round(scenario_vph, 1),
                    change_vph=round(change, 1),
                    change_percent=(
                        round(change / baseline_vph * 100, 1)
                        if baseline_vph >= 1
                        else None
                    ),
                    volume_capacity_ratio=round(scenario_vph / capacity, 3),
                    geometry=GeoJSONLineString(
                        coordinates=[
                            (float(longitude), float(latitude))
                            for longitude, latitude in geometry.coords
                        ]
                    ),
                )
            )
        records.sort(key=lambda record: abs(record.change_vph), reverse=True)
        residential_increase = sum(
            record.change_vph
            for record in records
            if record.change_vph > 0
            and record.road_class in {"residential", "living_street"}
        )
        changes = [record.change_vph for record in records]
        return (
            records[:limit],
            len(records),
            residential_increase,
            max([0.0, *changes]),
            min([0.0, *changes]),
        )

    def _assignment_progress(
        self,
        job_id: UUID,
        stage: str,
        completed: int,
        total: int,
    ) -> None:
        stage_start = 3 if stage == "baseline" else 49
        progress = stage_start + int(completed / max(total, 1) * 44)
        label = "normal network" if stage == "baseline" else "closure network"
        self._update(
            job_id,
            progress=progress,
            message=f"Assigning synthetic demand to the {label}.",
        )

    def _cancel_checker(self, job_id: UUID) -> Callable[[], None]:
        def check() -> None:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.cancel_event.is_set():
                    raise _Cancelled()

        return check

    def _update(
        self,
        job_id: UUID,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        result: DiversionResult | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress_percent = max(job.progress_percent, progress)
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            job.updated_at = datetime.now(PACIFIC)

    @staticmethod
    def _response(job: _Job) -> DiversionJobResponse:
        return DiversionJobResponse(
            id=job.id,
            status=job.status,
            progress_percent=job.progress_percent,
            message=job.message,
            cached=job.cached,
            created_at=job.created_at,
            updated_at=job.updated_at,
            result=job.result,
            error=job.error,
        )

    def _cache_key(self, payload: DiversionRequest) -> str:
        canonical = json.dumps(
            {
                "model_version": MODEL_VERSION,
                "request": payload.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cached_result(self, cache_key: str) -> DiversionResult | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM diversion_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return DiversionResult.model_validate_json(row["result_json"])

    def _store_cache(
        self,
        cache_key: str,
        payload: DiversionRequest,
        result: DiversionResult,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO diversion_cache (
                    cache_key, graph_version, model_version, request_json,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    result.graph_version,
                    MODEL_VERSION,
                    payload.model_dump_json(),
                    result.model_dump_json(),
                    datetime.now(PACIFIC).isoformat(),
                ),
            )
            connection.commit()


def _is_full_closure(
    edge: dict[str, Any],
    restrictions: dict[str, AppliedRestriction],
) -> bool:
    restriction = restrictions.get(str(edge["edge_id"]))
    return restriction is not None and restriction.type == "full"


def _effective_capacity(
    edge: dict[str, Any],
    restriction: AppliedRestriction | None,
) -> float:
    capacity = max(float(edge.get("estimated_capacity_vph", 800)), 1.0)
    if restriction and restriction.type == "lane" and restriction.remaining_lanes:
        capacity *= restriction.remaining_lanes / max(float(edge.get("lanes", 1)), 1.0)
    return max(capacity, 1.0)


def _congested_cost(
    edge: dict[str, Any],
    flow_vph: float,
    restriction: AppliedRestriction | None,
) -> float:
    base_cost, _travel_time = RoutingService._effective_costs(edge, restriction)
    capacity = _effective_capacity(edge, restriction)
    volume_capacity = max(flow_vph, 0.0) / capacity
    return base_cost * (1 + BPR_ALPHA * math.pow(volume_capacity, BPR_BETA))
