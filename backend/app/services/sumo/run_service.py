"""Bounded subprocess lifecycle for deterministic local SUMO runs."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import networkx as nx

from backend.app.core.settings import Settings
from backend.app.db.database import DatabaseManager
from backend.app.schemas.simulation import (
    SimulationRunCreated,
    SimulationRunRequest,
    SimulationRunStatus,
)
from backend.app.services.graph_service import GraphService


class SimulationRunNotFoundError(LookupError):
    pass


class SimulationRunConflictError(RuntimeError):
    pass


class SimulationRunService:
    def __init__(
        self,
        database: DatabaseManager,
        settings: Settings,
        graph_service: GraphService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.graph_service = graph_service
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def create(self, request: SimulationRunRequest) -> SimulationRunCreated:
        with self._lock:
            if any(process.poll() is None for process in self._processes.values()):
                raise SimulationRunConflictError("A local SUMO run is already active")
        sumo = shutil.which("sumo") or str(Path(sys.executable).parent / "sumo")
        netconvert = shutil.which("netconvert") or str(
            Path(sys.executable).parent / "netconvert"
        )
        run_id = str(uuid4())
        worker_request = self._worker_request(request, sumo, netconvert)
        run_identity = hashlib.sha256(
            json.dumps(worker_request, sort_keys=True).encode("utf-8")
        ).hexdigest()
        worker_request["application_run_id"] = run_id
        worker_request["run_identity"] = run_identity
        run_dir = (self.settings.sumo_runs_path / run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps(worker_request, indent=2) + "\n", encoding="utf-8")
        now = datetime.now(UTC).isoformat()
        run_key = run_identity
        graph_version, network_version, sumo_version = self._versions()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO simulation_runs
                (id, run_kind, status, run_key, seed, graph_version, sumo_version,
                 sumo_network_version, request_json, artifact_dir, progress, created_at)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    run_id,
                    request.run_kind,
                    run_key,
                    request.seed,
                    graph_version,
                    sumo_version,
                    network_version,
                    json.dumps(request.model_dump(mode="json")),
                    str(run_dir),
                    now,
                ),
            )
            connection.commit()
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.app.workers.sumo_worker", "--request", str(request_path)],
            cwd=Path.cwd(),
            stdout=(run_dir / "worker.stdout.log").open("w", encoding="utf-8"),
            stderr=(run_dir / "worker.stderr.log").open("w", encoding="utf-8"),
            text=True,
        )
        with self._lock:
            self._processes[run_id] = process
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE simulation_runs SET status='running', started_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), run_id),
            )
            connection.commit()
        threading.Thread(target=self._monitor, args=(run_id, process, run_dir), daemon=True).start()
        return SimulationRunCreated(id=UUID(run_id), status="running")

    def playback(self, run_id: UUID) -> dict[str, Any]:
        status = self.get(run_id)
        if status.status != "completed":
            raise SimulationRunConflictError("Simulation playback is not ready")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT artifact_dir FROM simulation_runs WHERE id=?", (str(run_id),)
            ).fetchone()
        playback_path = Path(row["artifact_dir"]) / "playback.json"
        if not playback_path.is_file():
            raise SimulationRunNotFoundError()
        return json.loads(playback_path.read_text(encoding="utf-8"))

    def get(self, run_id: UUID) -> SimulationRunStatus:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM simulation_runs WHERE id=?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise SimulationRunNotFoundError()
        progress = float(row["progress"])
        sim_second = row["current_sim_second"]
        progress_path = Path(row["artifact_dir"]) / "progress.json"
        if row["status"] in {"running", "cancel_requested"} and progress_path.exists():
            try:
                live = json.loads(progress_path.read_text(encoding="utf-8"))
                progress = float(live.get("progress", progress))
                sim_second = live.get("sim_second", sim_second)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return SimulationRunStatus(
            id=UUID(row["id"]),
            run_kind=row["run_kind"],
            status=row["status"],
            seed=row["seed"],
            progress=progress,
            current_sim_second=sim_second,
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            summary=json.loads(row["summary_json"]) if row["summary_json"] else None,
            error_message=row["error_message"],
        )

    def cancel(self, run_id: UUID) -> SimulationRunStatus:
        status = self.get(run_id)
        if status.status not in {"queued", "running", "cancel_requested"}:
            return status
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT artifact_dir FROM simulation_runs WHERE id=?", (str(run_id),)
            ).fetchone()
            Path(row["artifact_dir"], "cancel.requested").touch()
            connection.execute(
                "UPDATE simulation_runs SET status='cancel_requested' WHERE id=?", (str(run_id),)
            )
            connection.commit()
        return self.get(run_id)

    def active_run_id(self) -> str | None:
        with self._lock:
            return next(
                (run_id for run_id, process in self._processes.items() if process.poll() is None),
                None,
            )

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._processes.items())
        for run_id, process in active:
            if process.poll() is None:
                try:
                    self.get(UUID(run_id))
                    with self.database.connect() as connection:
                        row = connection.execute(
                            "SELECT artifact_dir FROM simulation_runs WHERE id=?", (run_id,)
                        ).fetchone()
                    Path(row["artifact_dir"], "cancel.requested").touch()
                    process.wait(timeout=3)
                except (Exception, subprocess.TimeoutExpired):  # noqa: BLE001
                    process.terminate()

    def _monitor(self, run_id: str, process: subprocess.Popen[str], run_dir: Path) -> None:
        return_code = process.wait()
        result_path = run_dir / "result.json"
        result: dict[str, Any] | None = None
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = None
        if result and result.get("status") in {"completed", "cancelled"}:
            status = str(result["status"])
            error = None
            progress = 1.0 if status == "completed" else 0.0
        else:
            status = "failed"
            error = (
                str(result.get("error"))
                if result and result.get("error")
                else f"SUMO worker exited with code {return_code}"
            )
            progress = 0.0
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE simulation_runs SET status=?, summary_json=?, progress=?,
                   finished_at=?, error_message=? WHERE id=?""",
                (
                    status,
                    json.dumps(result) if result else None,
                    progress,
                    datetime.now(UTC).isoformat(),
                    error,
                    run_id,
                ),
            )
            connection.commit()
        with self._lock:
            self._processes.pop(run_id, None)

    def _versions(self) -> tuple[str, str, str]:
        graph = json.loads(self.settings.graph_manifest_path.read_text(encoding="utf-8"))
        network = json.loads(
            self.settings.sumo_network_manifest_path.read_text(encoding="utf-8")
        )
        return (
            str(graph["graph_version"]),
            str(network["network_version"]),
            str(network["sumo_version"]),
        )

    def _worker_request(
        self,
        request: SimulationRunRequest,
        sumo_binary: str,
        netconvert_binary: str,
    ) -> dict[str, Any]:
        common = {
            "run_kind": request.run_kind,
            "seed": request.seed,
            "sumo_binary": sumo_binary,
            "step_delay_ms": request.step_delay_ms,
            "max_run_seconds": self.settings.sumo_max_run_seconds,
            "compute_chunk_seconds": self.settings.sumo_compute_chunk_seconds,
            "max_run_disk_mb": self.settings.sumo_max_run_disk_mb,
        }
        if request.run_kind == "validation":
            return {
                **common,
                "fixture_dir": str(Path("backend/tests/fixtures/sumo").resolve()),
                "netconvert_binary": netconvert_binary,
            }
        return {
            **common,
            "request": request.model_dump(mode="json"),
            "free_flow_floor_seconds": self._free_flow_floor_seconds(request),
            "network_path": str(self.settings.sumo_network_path.resolve()),
            "network_manifest_path": str(
                self.settings.sumo_network_manifest_path.resolve()
            ),
            "edge_map_path": str(self.settings.sumo_edge_map_path.resolve()),
            "gateway_connector_path": str(
                self.settings.sumo_gateway_connector_path.resolve()
            ),
            "station_cross_section_policy_path": str(
                self.settings.sumo_station_cross_section_policy_path.resolve()
            ),
            "graph_manifest_path": str(self.settings.graph_manifest_path.resolve()),
            "nodes_path": str(self.settings.graph_nodes_path.resolve()),
            "background_seed_directory": str(
                self.settings.proxy_od_seed_path.resolve().parent
            ),
            "demand_cache_directory": str(
                self.settings.sumo_runtime_demand_cache_path.resolve()
            ),
            "traffic_schedule_path": str(
                self.settings.traffic_schedule_path.resolve()
            ),
            "traffic_schedule_manifest_path": str(
                self.settings.traffic_schedule_manifest_path.resolve()
            ),
            "max_visible_vehicles": self.settings.sumo_playback_max_visible_vehicles,
        }

    def _free_flow_floor_seconds(self, request: SimulationRunRequest) -> float:
        if self.graph_service is None or self.graph_service.graph is None:
            raise SimulationRunConflictError("The active graph is required for a regional run")
        origin = self.graph_service.node_lookup.get(str(request.origin_node_id))
        destination = self.graph_service.node_lookup.get(str(request.destination_node_id))
        if origin is None or destination is None:
            raise SimulationRunConflictError("The selected trip nodes are not in the active graph")
        try:
            path = nx.shortest_path(
                self.graph_service.graph,
                origin,
                destination,
                weight="free_flow_seconds",
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound) as error:
            raise SimulationRunConflictError(
                "The selected trip has no physical free-flow path"
            ) from error
        seconds = 0.0
        for node_u, node_v in pairwise(path):
            edges = self.graph_service.graph.get_edge_data(node_u, node_v)
            seconds += min(float(edge["free_flow_seconds"]) for edge in edges.values())
        return round(seconds, 3)
