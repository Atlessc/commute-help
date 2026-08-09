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
    DiversionPlaybackEdge,
    DiversionJobResponse,
    DiversionRequest,
    DiversionResult,
)
from backend.app.schemas.routing import GeoJSONLineString, RouteCompareRequest
from backend.app.schemas.routing import RouteSummary
from backend.app.services.graph_service import GraphService, GraphUnavailableError
from backend.app.services.routing_service import (
    AppliedRestriction,
    InvalidClosureError,
    NoRouteError,
    RoutingService,
    SameLocationError,
)
from backend.app.services.spatial_service import SpatialService
from backend.app.services.traffic_service import (
    BackgroundTrafficSnapshot,
    TrafficProfileMismatchError,
    TrafficProfileNotFoundError,
    TrafficService,
)

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
MODEL_VERSION = "background-flow-msa-bpr-v3"
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


@dataclass(frozen=True)
class _Demand:
    origin: Any
    destination: Any
    volume_vph: float


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
        traffic_service: TrafficService,
    ) -> None:
        self.database = database
        self.graph_service = graph_service
        self.spatial_service = spatial_service
        self.routing_service = routing_service
        self.traffic_service = traffic_service
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
            TrafficProfileMismatchError,
            TrafficProfileNotFoundError,
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
        user_demands = [
            _Demand(origin=start, destination=end, volume_vph=payload.demand_vph / len(pairs))
            for start, end in pairs
        ]
        background = self._background_snapshot(payload)
        check_cancelled = self._cancel_checker(job_id)
        baseline = self._assign(
            graph=graph,
            demands=user_demands,
            iterations=payload.iterations,
            restrictions={},
            base_flows=background.flow_by_edge,
            reference_flows=background.flow_by_edge,
            observed_speeds=background.speed_kph_by_edge,
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
        scenario_background = dict(background.flow_by_edge)
        displaced_demands: list[_Demand] = []
        for edge_id, restriction in restrictions.items():
            if restriction.type != "full":
                continue
            displaced_vph = scenario_background.pop(edge_id, 0.0)
            position = self.graph_service.edge_id_positions.get(edge_id)
            if displaced_vph <= 0 or position is None or self.graph_service.edges is None:
                continue
            u, v, _key = self.graph_service.edges.index[position]
            displaced_demands.append(
                _Demand(origin=u, destination=v, volume_vph=displaced_vph)
            )
        scenario = self._assign(
            graph=scenario_graph,
            demands=[*user_demands, *displaced_demands],
            iterations=payload.iterations,
            restrictions=restrictions,
            base_flows=scenario_background,
            reference_flows=background.flow_by_edge,
            observed_speeds=background.speed_kph_by_edge,
            progress=lambda completed, total: self._assignment_progress(
                job_id, "scenario", completed, total
            ),
            check_cancelled=check_cancelled,
        )
        check_cancelled()
        self._update(job_id, progress=97, message="Preparing the spillover layer.")
        recommended_route = self._recommended_route(
            scenario_graph,
            origin,
            destination,
            scenario.flows,
            background.flow_by_edge,
            background.speed_kph_by_edge,
            restrictions,
            manifest.graph_version,
        )
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
            traffic_profile_id=payload.traffic_profile_id,
            background_source=background.source_name,
            background_bucket=background.bucket_label,
            background_observation_count=background.observation_count,
            background_matched_edge_count=background.matched_edge_count,
            background_network_coverage_percent=background.coverage_percent,
            displaced_background_vph=sum(demand.volume_vph for demand in displaced_demands),
            assigned_demand_vph=scenario.assigned_vph,
            unassigned_demand_vph=scenario.unassigned_vph,
            changed_edge_count=changed_count,
            max_increase_vph=max_increase,
            max_decrease_vph=max_decrease,
            residential_increase_vph=max(0.0, residential_increase),
            recommended_route=recommended_route,
            edge_changes=edge_changes,
            playback_edges=self._playback_edges(
                baseline.flows,
                scenario.flows,
                restrictions,
            ),
            assumptions=[
                (
                    "Matched historical volume and speed seed the modeled network; uncovered roads remain class-based estimates."
                    if payload.traffic_profile_id
                    else "No historical background-flow profile was selected; traffic demand is synthetic."
                ),
                "Demand pairs are deterministically dispersed around the selected trip endpoints.",
                "Routes use directed OpenStreetMap access, class penalties, and estimated hourly capacities.",
                "Congestion costs use the uncalibrated BPR formula with alpha 0.15 and beta 4.0.",
                "Incremental all-or-nothing assignments are averaged with the method of successive averages.",
                "Observed flow on a fully closed directed edge is reassigned between that edge's endpoints as a bounded first-order detour.",
                "Playback dots are bounded visual samples tweened between computed baseline and scenario road-flow targets; they are not one dot per observed vehicle.",
                "Regional origin-destination demand, signal timing, and queue spillback are not yet modeled.",
            ],
        )

    def _background_snapshot(self, payload: DiversionRequest) -> BackgroundTrafficSnapshot:
        manifest = self.graph_service.require_manifest()
        if payload.traffic_profile_id is not None:
            return self.traffic_service.background_snapshot(
                payload.traffic_profile_id,
                payload.departure_time,
            )
        return BackgroundTrafficSnapshot(
            profile_id=UUID(int=0),
            profile_version="synthetic-background-v1",
            source_name="Synthetic demand only",
            source_window="No historical background observations",
            bucket_label="Selected departure time",
            flow_by_edge={},
            speed_kph_by_edge={},
            observation_count=0,
            matched_edge_count=0,
            network_edge_count=manifest.metrics.directed_edges,
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
        demands: list[_Demand],
        iterations: int,
        restrictions: dict[str, AppliedRestriction],
        base_flows: dict[str, float],
        reference_flows: dict[str, float],
        observed_speeds: dict[str, float],
        progress: Callable[[int, int], None],
        check_cancelled: Callable[[], None],
    ) -> _Assignment:
        assigned_flows: dict[str, float] = {}
        total_steps = iterations * len(demands)
        completed_steps = 0
        final_unassigned_vph = 0.0
        for iteration in range(1, iterations + 1):
            auxiliary: dict[str, float] = defaultdict(float)
            unassigned_vph = 0.0
            total_flows = _merge_flows(base_flows, assigned_flows)
            weight = self._congested_weight(
                total_flows,
                restrictions,
                reference_flows,
                observed_speeds,
            )
            for demand in demands:
                check_cancelled()
                try:
                    path = self.routing_service.find_path(
                        graph, demand.origin, demand.destination, weight=weight
                    )
                except NoRouteError:
                    unassigned_vph += demand.volume_vph
                else:
                    for u, v in zip(path, path[1:]):
                        edge = self._best_assignment_edge(
                            graph,
                            u,
                            v,
                            total_flows,
                            restrictions,
                            reference_flows,
                            observed_speeds,
                        )
                        auxiliary[str(edge["edge_id"])] += demand.volume_vph
                completed_steps += 1
                progress(completed_steps, total_steps)
            step_size = 1 / iteration
            for edge_id in set(assigned_flows) | set(auxiliary):
                assigned_flows[edge_id] = assigned_flows.get(edge_id, 0.0) + step_size * (
                    auxiliary.get(edge_id, 0.0) - assigned_flows.get(edge_id, 0.0)
                )
            final_unassigned_vph = unassigned_vph
        demand_vph = sum(demand.volume_vph for demand in demands)
        return _Assignment(
            flows=_merge_flows(base_flows, assigned_flows),
            assigned_vph=max(0.0, demand_vph - final_unassigned_vph),
            unassigned_vph=final_unassigned_vph,
        )

    @staticmethod
    def _congested_weight(
        flows: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
        reference_flows: dict[str, float],
        observed_speeds: dict[str, float],
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
                    reference_flows.get(str(edge["edge_id"]), 0.0),
                    observed_speeds.get(str(edge["edge_id"])),
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
        reference_flows: dict[str, float],
        observed_speeds: dict[str, float],
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
                reference_flows.get(str(edge["edge_id"]), 0.0),
                observed_speeds.get(str(edge["edge_id"])),
            ),
        )

    def _recommended_route(
        self,
        graph: nx.MultiDiGraph,
        origin: Any,
        destination: Any,
        flows: dict[str, float],
        reference_flows: dict[str, float],
        observed_speeds: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
        graph_version: str,
    ) -> RouteSummary | None:
        weight = self._congested_weight(
            flows,
            restrictions,
            reference_flows,
            observed_speeds,
        )
        try:
            path = self.routing_service.find_path(
                graph,
                origin,
                destination,
                weight=weight,
            )
        except NoRouteError:
            return None

        coordinates: list[tuple[float, float]] = []
        distance_m = 0.0
        travel_time_seconds = 0.0
        edge_identity: list[str] = []
        for u, v in zip(path, path[1:]):
            edge = self._best_assignment_edge(
                graph,
                u,
                v,
                flows,
                restrictions,
                reference_flows,
                observed_speeds,
            )
            edge_id = str(edge["edge_id"])
            edge_coordinates = self.routing_service._oriented_coordinates(
                graph, u, v, edge
            )
            if coordinates and edge_coordinates and coordinates[-1] == edge_coordinates[0]:
                coordinates.extend(edge_coordinates[1:])
            else:
                coordinates.extend(edge_coordinates)
            distance_m += float(edge["length_m"])
            travel_time_seconds += _congested_travel_time(
                edge,
                flows.get(edge_id, 0.0),
                restrictions.get(edge_id),
                reference_flows.get(edge_id, 0.0),
                observed_speeds.get(edge_id),
            )
            edge_identity.append(edge_id)
        route_id = hashlib.sha256(
            json.dumps(
                [MODEL_VERSION, graph_version, str(origin), str(destination), edge_identity],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return RouteSummary(
            route_id=route_id,
            travel_time_seconds=round(travel_time_seconds, 1),
            distance_m=round(distance_m, 1),
            geometry=GeoJSONLineString(coordinates=coordinates),
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

    def _playback_edges(
        self,
        baseline: dict[str, float],
        scenario: dict[str, float],
        restrictions: dict[str, AppliedRestriction],
        limit: int = 450,
    ) -> list[DiversionPlaybackEdge]:
        """Return high-flow road geometries for bounded client-side tweening."""

        edges = self.graph_service.edges
        graph = self.graph_service.graph
        if edges is None or graph is None:
            raise GraphUnavailableError("Graph edge details are unavailable.")
        ranked_ids = sorted(
            set(baseline) | set(scenario),
            key=lambda edge_id: max(
                baseline.get(edge_id, 0.0),
                scenario.get(edge_id, 0.0),
            ),
            reverse=True,
        )
        records: list[DiversionPlaybackEdge] = []
        for edge_id in ranked_ids:
            if len(records) >= limit:
                break
            position = self.graph_service.edge_id_positions.get(edge_id)
            if position is None:
                continue
            u, v, key = edges.index[position]
            edge = graph.edges[u, v, key]
            geometry = edge.get("geometry")
            if geometry is None or len(geometry.coords) < 2:
                continue
            scenario_vph = max(0.0, scenario.get(edge_id, 0.0))
            capacity = _effective_capacity(edge, restrictions.get(edge_id))
            records.append(
                DiversionPlaybackEdge(
                    edge_id=edge_id,
                    baseline_vph=round(max(0.0, baseline.get(edge_id, 0.0)), 1),
                    scenario_vph=round(scenario_vph, 1),
                    volume_capacity_ratio=round(scenario_vph / capacity, 3),
                    geometry=GeoJSONLineString(
                        coordinates=[
                            (float(longitude), float(latitude))
                            for longitude, latitude in geometry.coords
                        ]
                    ),
                )
            )
        return records

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
    reference_flow_vph: float = 0.0,
    observed_speed_kph: float | None = None,
) -> float:
    base_cost, effective_travel_time = RoutingService._effective_costs(edge, restriction)
    calibrated_travel_time = _calibrated_travel_time(
        edge,
        effective_travel_time,
        observed_speed_kph,
    )
    base_cost *= calibrated_travel_time / max(effective_travel_time, 0.1)
    capacity = _effective_capacity(edge, restriction)
    return base_cost * _relative_bpr_factor(
        flow_vph,
        reference_flow_vph,
        capacity,
    )


def _congested_travel_time(
    edge: dict[str, Any],
    flow_vph: float,
    restriction: AppliedRestriction | None,
    reference_flow_vph: float = 0.0,
    observed_speed_kph: float | None = None,
) -> float:
    _cost, effective_travel_time = RoutingService._effective_costs(edge, restriction)
    calibrated_travel_time = _calibrated_travel_time(
        edge,
        effective_travel_time,
        observed_speed_kph,
    )
    return calibrated_travel_time * _relative_bpr_factor(
        flow_vph,
        reference_flow_vph,
        _effective_capacity(edge, restriction),
    )


def _calibrated_travel_time(
    edge: dict[str, Any],
    effective_travel_time: float,
    observed_speed_kph: float | None,
) -> float:
    if observed_speed_kph is None or observed_speed_kph <= 0:
        return effective_travel_time
    observed_seconds = float(edge["length_m"]) / (observed_speed_kph / 3.6)
    return max(effective_travel_time, observed_seconds)


def _relative_bpr_factor(
    flow_vph: float,
    reference_flow_vph: float,
    capacity_vph: float,
) -> float:
    current_ratio = max(flow_vph, 0.0) / max(capacity_vph, 1.0)
    reference_ratio = max(reference_flow_vph, 0.0) / max(capacity_vph, 1.0)
    current = 1 + BPR_ALPHA * math.pow(current_ratio, BPR_BETA)
    reference = 1 + BPR_ALPHA * math.pow(reference_ratio, BPR_BETA)
    return current / reference


def _merge_flows(
    first: dict[str, float],
    second: dict[str, float],
) -> dict[str, float]:
    return {
        edge_id: first.get(edge_id, 0.0) + second.get(edge_id, 0.0)
        for edge_id in set(first) | set(second)
    }
