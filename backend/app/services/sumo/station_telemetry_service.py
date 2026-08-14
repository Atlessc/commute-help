"""Optional checkpoint-safe regional station crossing telemetry."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.sumo_station_cross_section_policy import (
    POLICY_NAME,
    POLICY_VERSION,
    StationCrossSectionPolicyManifestV1,
)
from backend.app.schemas.sumo_station_telemetry import (
    STATION_OBSERVATION_PLAN_VERSION,
    STATION_OBSERVER_ALGORITHM,
    STATION_OBSERVER_IMPLEMENTATION,
    STATION_OBSERVER_STATE_VERSION,
    STATION_TELEMETRY_PRODUCER_VERSION,
    StationTelemetryManifestV1,
    StationTelemetryRecordV1,
    StationTelemetryRecoveryManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.departure_provenance_service import (
    DEPARTURE_FRAGMENT_VERSION,
    DepartureProvenanceError,
)

STATION_TELEMETRY_DIRECTORY = "station-telemetry-15m"
STATION_TELEMETRY_PARQUET = "station-telemetry-15m.parquet"
STATION_TELEMETRY_MANIFEST = "station-telemetry-manifest.json"
STATION_TELEMETRY_RECOVERY = "station-telemetry-recovery.json"
STATION_FRAGMENT = "station-telemetry-fragment.json"
STATION_PLAN = "station-observation-plan.json"
STATION_ROUTE_CANDIDATES = "station-route-candidates.json"
_VARIANTS = ("baseline", "scenario")


class StationTelemetryError(RuntimeError):
    """Station observation provenance or telemetry is invalid."""


@dataclass(frozen=True)
class ProductionStationDefinition:
    station_id: str
    app_edge_id: str
    cross_section_type: Literal["within_edge_position", "edge_transition"]
    sumo_edge_id: str | None
    position_m: float | None
    transition_from_edge_id: str | None
    transition_to_edge_id: str | None
    direction: str
    relation_id: str
    policy_row_id: str


def build_station_observation_plan(
    *,
    policy_directory: Path,
    expected_policy_digest: str,
    network_path: Path,
) -> dict[str, Any]:
    """Load exactly the reviewed flow-eligible policy rows into plain Python."""
    manifest = StationCrossSectionPolicyManifestV1.model_validate_json(
        (policy_directory / "station-cross-section-policy-manifest.json").read_text()
    )
    if manifest.content_digest != expected_policy_digest:
        raise StationTelemetryError("Station policy digest does not match the request")
    rows = pq.read_table(
        policy_directory / manifest.policy_output.relative_path,
        columns=[
            "policy_row_id",
            "station_id",
            "app_edge_id",
            "cross_section_type",
            "sumo_edge_id",
            "projected_position_m",
            "transition_from_sumo_edge_id",
            "transition_to_sumo_edge_id",
            "direction",
            "relation_id",
            "flow_measurement_eligible",
        ],
    ).to_pylist()
    accepted = [row for row in rows if row["flow_measurement_eligible"] is True]
    if len(accepted) != 356:
        raise StationTelemetryError(
            f"Phase 2.2e requires exactly 356 accepted flow stations, found {len(accepted)}"
        )
    stations = []
    for row in sorted(accepted, key=lambda value: str(value["station_id"])):
        cross_type = str(row["cross_section_type"])
        station = ProductionStationDefinition(
            station_id=str(row["station_id"]),
            app_edge_id=str(row["app_edge_id"]),
            cross_section_type=cross_type,  # type: ignore[arg-type]
            sumo_edge_id=(str(row["sumo_edge_id"]) if row["sumo_edge_id"] else None),
            position_m=(
                float(row["projected_position_m"])
                if cross_type == "within_edge_position"
                else None
            ),
            transition_from_edge_id=(
                str(row["transition_from_sumo_edge_id"])
                if row["transition_from_sumo_edge_id"]
                else None
            ),
            transition_to_edge_id=(
                str(row["transition_to_sumo_edge_id"])
                if row["transition_to_sumo_edge_id"]
                else None
            ),
            direction=str(row["direction"]),
            relation_id=str(row["relation_id"]),
            policy_row_id=str(row["policy_row_id"]),
        )
        _validate_station_definition(station)
        stations.append(asdict(station))

    station_edges = {
        str(value)
        for station in stations
        for value in (
            station["sumo_edge_id"],
            station["transition_from_edge_id"],
            station["transition_to_edge_id"],
        )
        if value
    }
    predecessors = _network_predecessors(network_path, station_edges)
    relevant_edges = sorted(station_edges | set(predecessors))
    semantic = {
        "plan_version": STATION_OBSERVATION_PLAN_VERSION,
        "policy_name": POLICY_NAME,
        "policy_version": POLICY_VERSION,
        "policy_content_digest": manifest.content_digest,
        "observer_algorithm": STATION_OBSERVER_ALGORITHM,
        "observer_implementation": STATION_OBSERVER_IMPLEMENTATION,
        "stations": stations,
        "relevant_edge_ids": relevant_edges,
        "station_edge_predecessors": sorted(predecessors),
    }
    semantic["observation_plan_digest"] = hashlib.sha256(
        canonical_json(semantic).encode()
    ).hexdigest()
    return semantic


def write_station_observation_plan(path: Path, plan: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(canonical_json(plan) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_station_observation_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    claimed = str(plan.pop("observation_plan_digest"))
    actual = hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    plan["observation_plan_digest"] = claimed
    if actual != claimed or len(plan.get("stations", [])) != 356:
        raise StationTelemetryError("Station observation plan identity is invalid")
    return plan


def numeric_departure_positions(route_path: Path) -> dict[str, float]:
    """Return only authoritative numeric departPos values; never resolve random_free."""
    result: dict[str, float] = {}
    open_source = gzip.open if route_path.suffix == ".gz" else Path.open
    with open_source(route_path, "rb") as source:
        for _, element in ET.iterparse(source, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] not in {"vehicle", "trip"}:
                continue
            vehicle_id = element.attrib.get("id")
            raw = element.attrib.get("departPos")
            if vehicle_id and raw:
                try:
                    result[vehicle_id] = float(raw)
                except ValueError:
                    pass
            element.clear()
    return result


def build_route_candidate_provenance(
    *, route_path: Path, relevant_edge_ids: set[str]
) -> dict[str, Any]:
    """Select only routed vehicles that can encounter an observed cross-section."""
    candidates: set[str] = set()
    numeric_positions: dict[str, float] = {}
    route_count = 0
    open_source = gzip.open if route_path.suffix == ".gz" else Path.open
    with open_source(route_path, "rb") as source:
        for _, element in ET.iterparse(source, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "vehicle":
                continue
            vehicle_id = element.attrib.get("id")
            route = next(
                (child for child in element if child.tag.rsplit("}", 1)[-1] == "route"),
                None,
            )
            route_edges = (
                str(route.attrib.get("edges", "")).split() if route is not None else []
            )
            route_count += 1
            if vehicle_id and any(edge in relevant_edge_ids for edge in route_edges):
                candidates.add(vehicle_id)
                raw_position = element.attrib.get("departPos")
                if raw_position:
                    try:
                        numeric_positions[vehicle_id] = float(raw_position)
                    except ValueError:
                        pass
            element.clear()
    semantic = {
        "schema_version": 1,
        "route_sha256": _sha256(route_path),
        "route_vehicle_count": route_count,
        "candidate_vehicle_ids": sorted(candidates),
        "numeric_departure_positions_m": dict(sorted(numeric_positions.items())),
        "candidate_selection": "route_intersects_station_or_predecessor_edge",
    }
    semantic["content_digest"] = hashlib.sha256(
        canonical_json(semantic).encode()
    ).hexdigest()
    return semantic


def write_route_candidate_provenance(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_route_candidate_provenance(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    claimed = value.pop("content_digest")
    actual = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    value["content_digest"] = claimed
    if actual != claimed:
        raise StationTelemetryError("Station route-candidate identity is invalid")
    return value


class OptimizedStationObserver:
    """Bounded station observer consuming one batched subscription result per step."""

    def __init__(
        self,
        plan: dict[str, Any],
        *,
        variant: str,
        restored_state: dict[str, Any] | None = None,
        defer_interval_finalization: bool = False,
    ) -> None:
        self.plan = plan
        self.variant = variant
        self.stations = [ProductionStationDefinition(**row) for row in plan["stations"]]
        self.relevant_edges = set(plan["relevant_edge_ids"])
        self.previous: dict[str, dict[str, Any]] = {}
        self.emitted: set[str] = set()
        self.accumulators: dict[str, dict[str, dict[str, Any]]] = {}
        self.metrics = _zero_metrics()
        self.pending_direct_departures: dict[str, dict[str, Any]] = {}
        self.departure_provenance: dict[str, dict[str, Any]] = {}
        self.defer_interval_finalization = defer_interval_finalization
        if restored_state:
            self._restore(restored_state)

    def observe(
        self,
        *,
        time_seconds: int,
        subscription_rows: dict[str, dict[str, Any]],
        departed_ids: set[str],
        arrived_ids: set[str],
        departure_positions_m: dict[str, float],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        current: dict[str, dict[str, Any]] = {}
        for vehicle_id, raw in sorted(subscription_rows.items()):
            self.metrics["subscription_result_rows"] += 1
            road_id = str(raw["road_id"])
            if road_id not in self.relevant_edges:
                continue
            current[vehicle_id] = {
                "road_id": road_id,
                "lane_position_m": float(raw["lane_position_m"]),
                "route_index": int(raw["route_index"]),
                "time_seconds": int(time_seconds),
            }
        self.metrics["relevant_vehicle_observations"] += len(current)
        self.metrics["peak_relevant_state_count"] = max(
            self.metrics["peak_relevant_state_count"], len(current)
        )
        events = observe_production_crossings(
            previous=self.previous,
            current=current,
            departed_ids=departed_ids,
            departure_positions_m=departure_positions_m,
            stations=self.stations,
            emitted=self.emitted,
        )
        self.metrics["crossing_event_count"] += len(events)
        for event in events:
            self._record_crossing(event)
        self._capture_direct_departure_candidates(
            current=current,
            departed_ids=departed_ids,
            departure_positions_m=departure_positions_m,
        )
        # Only relevant observations survive. Emitted identities are pruned with
        # active vehicle IDs and current route occurrences, so state never grows
        # with the historical campaign.
        self.previous = current
        live_ids = set(subscription_rows) - arrived_ids
        self.emitted = {
            value for value in self.emitted if value.split("|", 1)[0] in live_ids
        }
        completed = (
            []
            if self.defer_interval_finalization
            else self._finalize_before(time_seconds)
        )
        return events, completed

    def reconcile_native_departures(
        self,
        *,
        records: list[dict[str, Any]],
        missing_numeric_vehicle_ids: set[str],
        time_seconds: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve departure-step candidates before checkpoint promotion."""
        by_id = {str(record["vehicle_id"]): dict(record) for record in records}
        if len(by_id) != len(records):
            raise DepartureProvenanceError(
                "Duplicate native departure records in child"
            )
        events: list[dict[str, Any]] = []
        for vehicle_id in sorted(set(by_id) | missing_numeric_vehicle_ids):
            pending = self.pending_direct_departures.pop(vehicle_id, None)
            if pending is None:
                continue
            record = by_id.get(vehicle_id)
            for candidate in pending["station_candidates"]:
                bucket = self._accumulator_bucket(
                    int(pending["time_seconds"]), str(candidate["station_id"])
                )
                if record is None or str(record["departure_edge_id"]) != str(
                    pending["road_id"]
                ):
                    bucket["unresolved_direct_departure_count"] += 1
                    self.metrics["unresolved_direct_departure_count"] += 1
                    continue
                bucket["resolved_direct_departure_count"] += 1
                self.metrics["resolved_direct_departure_count"] += 1
                self.departure_provenance[vehicle_id] = record
                station_position = float(candidate["position_m"])
                if not (
                    float(record["actual_departure_position_m"])
                    <= station_position
                    <= float(pending["first_observed_position_m"])
                ):
                    continue
                occurrence = (
                    f"{vehicle_id}|{candidate['station_id']}|{pending['route_index']}"
                )
                if occurrence in self.emitted:
                    continue
                self.emitted.add(occurrence)
                event = {
                    "event_id": hashlib.sha256(occurrence.encode()).hexdigest(),
                    "vehicle_id": vehicle_id,
                    "station_id": candidate["station_id"],
                    "route_index": int(pending["route_index"]),
                    "crossing_method": "direct_departure_native_position",
                    "observation_time_seconds": int(pending["time_seconds"]),
                }
                events.append(event)
                self._record_crossing(event)
        self.metrics["crossing_event_count"] += len(events)
        return events, self._finalize_before(time_seconds)

    def checkpoint_state(self) -> dict[str, Any]:
        accumulators = {
            interval: {
                station: {
                    "count": int(value["count"]),
                    "vehicle_ids": sorted(value["vehicle_ids"]),
                    "resolved_direct_departure_count": int(
                        value.get("resolved_direct_departure_count", 0)
                    ),
                    "unresolved_direct_departure_count": int(
                        value.get("unresolved_direct_departure_count", 0)
                    ),
                }
                for station, value in sorted(stations.items())
            }
            for interval, stations in sorted(
                self.accumulators.items(), key=lambda x: int(x[0])
            )
        }
        return {
            "schema_version": STATION_OBSERVER_STATE_VERSION,
            "observer_algorithm": STATION_OBSERVER_ALGORITHM,
            "observer_implementation": STATION_OBSERVER_IMPLEMENTATION,
            "observation_plan_digest": self.plan["observation_plan_digest"],
            "previous": self.previous,
            "emitted": sorted(self.emitted),
            "partial_interval_accumulators": accumulators,
            "pending_direct_departures": self.pending_direct_departures,
            "departure_provenance": self.departure_provenance,
            "metrics": self.metrics,
        }

    def _restore(self, state: dict[str, Any]) -> None:
        if (
            state.get("schema_version") != STATION_OBSERVER_STATE_VERSION
            or state.get("observation_plan_digest")
            != self.plan["observation_plan_digest"]
        ):
            raise StationTelemetryError("Observer checkpoint state is incompatible")
        self.previous = {str(k): dict(v) for k, v in state.get("previous", {}).items()}
        self.emitted = set(state.get("emitted", []))
        for interval, stations in state.get(
            "partial_interval_accumulators", {}
        ).items():
            self.accumulators[str(interval)] = {
                str(station): {
                    "count": int(value["count"]),
                    "vehicle_ids": set(value["vehicle_ids"]),
                    "resolved_direct_departure_count": int(
                        value.get("resolved_direct_departure_count", 0)
                    ),
                    "unresolved_direct_departure_count": int(
                        value.get("unresolved_direct_departure_count", 0)
                    ),
                }
                for station, value in stations.items()
            }
        self.metrics = {**_zero_metrics(), **state.get("metrics", {})}
        self.pending_direct_departures = {
            str(key): dict(value)
            for key, value in state.get("pending_direct_departures", {}).items()
        }
        self.departure_provenance = {
            str(key): dict(value)
            for key, value in state.get("departure_provenance", {}).items()
        }

    def _capture_direct_departure_candidates(
        self,
        *,
        current: dict[str, dict[str, Any]],
        departed_ids: set[str],
        departure_positions_m: dict[str, float],
    ) -> None:
        for vehicle_id in sorted(departed_ids & set(current)):
            now = current[vehicle_id]
            candidates = [
                {"station_id": station.station_id, "position_m": station.position_m}
                for station in self.stations
                if station.cross_section_type == "within_edge_position"
                and station.sumo_edge_id == now["road_id"]
                and float(station.position_m) <= float(now["lane_position_m"])
            ]
            if not candidates:
                continue
            if vehicle_id in departure_positions_m:
                for candidate in candidates:
                    self._accumulator_bucket(
                        int(now["time_seconds"]), str(candidate["station_id"])
                    )["resolved_direct_departure_count"] += 1
                    self.metrics["resolved_direct_departure_count"] += 1
                continue
            self.metrics["relevant_direct_departure_count"] += 1
            self.pending_direct_departures[vehicle_id] = {
                "road_id": now["road_id"],
                "first_observed_position_m": now["lane_position_m"],
                "route_index": now["route_index"],
                "time_seconds": now["time_seconds"],
                "station_candidates": candidates,
            }

    def _accumulator_bucket(self, time_seconds: int, station_id: str) -> dict[str, Any]:
        interval_start = (int(time_seconds) // 900) * 900
        interval = self.accumulators.setdefault(str(interval_start), {})
        return interval.setdefault(
            station_id,
            {
                "count": 0,
                "vehicle_ids": set(),
                "resolved_direct_departure_count": 0,
                "unresolved_direct_departure_count": 0,
            },
        )

    def _record_crossing(self, event: dict[str, Any]) -> None:
        bucket = self._accumulator_bucket(
            int(event["observation_time_seconds"]), str(event["station_id"])
        )
        bucket["count"] += 1
        bucket["vehicle_ids"].add(str(event["vehicle_id"]))

    def _finalize_before(self, time_seconds: int) -> list[dict[str, Any]]:
        current_start = (int(time_seconds) // 900) * 900
        completed: list[dict[str, Any]] = []
        for raw_start in sorted(self.accumulators, key=int):
            start = int(raw_start)
            if start >= current_start:
                continue
            completed.extend(self._dense_rows(start, self.accumulators.pop(raw_start)))
        # Empty intervals must also be explicit. At each exact boundary create
        # the previous dense zero interval even when no crossing ever occurred.
        if time_seconds > 0 and time_seconds % 900 == 0:
            start = time_seconds - 900
            if not any(row["interval_start_seconds"] == start for row in completed):
                completed.extend(self._dense_rows(start, {}))
        if current_start > 0:
            self.departure_provenance = {
                vehicle_id: record
                for vehicle_id, record in self.departure_provenance.items()
                if float(record["actual_departure_time_seconds"]) >= current_start
            }
        return completed

    def _dense_rows(
        self, start: int, accumulators: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = []
        for station in self.stations:
            value = accumulators.get(station.station_id, {})
            count = int(value.get("count", 0))
            vehicle_ids = value.get("vehicle_ids", set())
            resolved = int(value.get("resolved_direct_departure_count", 0))
            unresolved = int(value.get("unresolved_direct_departure_count", 0))
            rows.append(
                {
                    "variant": self.variant,
                    "station_id": station.station_id,
                    "app_edge_id": station.app_edge_id,
                    "cross_section_type": station.cross_section_type,
                    "sumo_edge_id": station.sumo_edge_id,
                    "transition_from_sumo_edge_id": station.transition_from_edge_id,
                    "transition_to_sumo_edge_id": station.transition_to_edge_id,
                    "interval_start_seconds": start,
                    "interval_end_seconds": start + 900,
                    "crossing_count": count,
                    "flow_vph": count * 4.0,
                    "contributing_vehicle_count": len(vehicle_ids),
                    "resolved_direct_departure_count": resolved,
                    "unresolved_direct_departure_count": unresolved,
                    "flow_measurement_complete": unresolved == 0,
                }
            )
        return rows


def observe_production_crossings(
    *,
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    departed_ids: set[str],
    departure_positions_m: dict[str, float],
    stations: list[ProductionStationDefinition],
    emitted: set[str],
) -> list[dict[str, Any]]:
    """Exact flow event predicate shared by reference and optimized paths."""
    events = []
    for vehicle_id, now in sorted(current.items()):
        before = previous.get(vehicle_id)
        road_id = str(now["road_id"])
        current_position = float(now["lane_position_m"])
        route_index = int(now["route_index"])
        for station in stations:
            crossed = False
            method: str | None = None
            if station.cross_section_type == "within_edge_position":
                if road_id != station.sumo_edge_id:
                    continue
                if before is not None and str(before["road_id"]) == road_id:
                    crossed = (
                        float(before["lane_position_m"])
                        < float(station.position_m)
                        <= current_position
                    )
                    method = "same_edge_position" if crossed else None
                elif before is not None:
                    crossed = (
                        route_index > int(before["route_index"])
                        and float(station.position_m) <= current_position
                    )
                    method = "edge_transition_into_station_edge" if crossed else None
                elif vehicle_id in departed_ids:
                    depart_pos = departure_positions_m.get(vehicle_id)
                    crossed = (
                        depart_pos is not None
                        and depart_pos <= float(station.position_m) <= current_position
                    )
                    method = "direct_departure_numeric_position" if crossed else None
            else:
                crossed = bool(
                    before is not None
                    and str(before["road_id"]) == station.transition_from_edge_id
                    and road_id == station.transition_to_edge_id
                    and route_index > int(before["route_index"])
                )
                method = "accepted_edge_transition" if crossed else None
            if not crossed:
                continue
            occurrence = f"{vehicle_id}|{station.station_id}|{route_index}"
            if occurrence in emitted:
                continue
            emitted.add(occurrence)
            events.append(
                {
                    "event_id": hashlib.sha256(occurrence.encode()).hexdigest(),
                    "vehicle_id": vehicle_id,
                    "station_id": station.station_id,
                    "route_index": route_index,
                    "crossing_method": method,
                    "observation_time_seconds": int(now["time_seconds"]),
                }
            )
    return events


def write_station_fragment(path: Path, rows: list[dict[str, Any]]) -> None:
    value = {"schema_version": 1, "rows": sorted(rows, key=_row_sort_key)}
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def materialize_station_telemetry(
    *,
    run_dir: Path,
    application_run_id: str,
    run_identity: str,
    network_manifest_path: Path,
    graph_manifest_path: Path,
    demand_manifest: dict[str, Any],
    plan: dict[str, Any],
    seed: int,
    simulation_end_seconds: int,
    recovery_provenance: dict[str, Any] | None = None,
) -> StationTelemetryManifestV1:
    """Atomically promote dense complete station intervals from checkpoints."""
    rows = []
    for variant in _VARIANTS:
        root = run_dir / "checkpoints" / variant
        for checkpoint in sorted(root.iterdir(), key=lambda value: value.name):
            if not checkpoint.is_dir() or not checkpoint.name.isdigit():
                continue
            fragment = checkpoint / STATION_FRAGMENT
            if not fragment.is_file():
                raise StationTelemetryError(f"Missing station fragment in {checkpoint}")
            value = json.loads(fragment.read_text())
            rows.extend(value["rows"])
    rows.sort(key=_row_sort_key)
    complete_intervals = simulation_end_seconds // 900
    expected = 356 * complete_intervals * 2
    identities = {
        (row["variant"], row["station_id"], row["interval_start_seconds"])
        for row in rows
    }
    if len(rows) != expected or len(identities) != expected:
        raise StationTelemetryError("Station fragments are incomplete or duplicated")
    network = json.loads(network_manifest_path.read_text())
    graph = json.loads(graph_manifest_path.read_text())
    output_rows = []
    for row in rows:
        output_rows.append(
            StationTelemetryRecordV1(
                schema_version=1,
                run_identity=run_identity,
                network_version=network["network_version"],
                sumo_version=network["sumo_version"],
                simulation_mode="mesoscopic",
                seed=seed,
                demand_version=demand_manifest["demand_version"],
                **row,
                interval_duration_seconds=900,
                crossing_step_speed_sample_count=0,
                policy_name=POLICY_NAME,
                policy_version=POLICY_VERSION,
                policy_content_digest=plan["policy_content_digest"],
                observation_plan_digest=plan["observation_plan_digest"],
                observer_algorithm=STATION_OBSERVER_ALGORITHM,
                observer_implementation=STATION_OBSERVER_IMPLEMENTATION,
                evidence_level="modeled_uncalibrated",
                calibration_status="not_calibrated",
            ).model_dump()
        )
    pending = run_dir / f".{STATION_TELEMETRY_DIRECTORY}.pending"
    final = run_dir / STATION_TELEMETRY_DIRECTORY
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir()
    output_path = pending / STATION_TELEMETRY_PARQUET
    table = pa.Table.from_pylist(output_rows, schema=_arrow_schema())
    pq.write_table(table, output_path, compression="zstd", use_dictionary=True)
    row_digest = hashlib.sha256(
        "".join(canonical_json(row) + "\n" for row in output_rows).encode()
    ).hexdigest()
    content = {
        "producer_version": STATION_TELEMETRY_PRODUCER_VERSION,
        "run_identity": run_identity,
        "policy_content_digest": plan["policy_content_digest"],
        "observation_plan_digest": plan["observation_plan_digest"],
        "row_content_sha256": row_digest,
        "row_count": len(output_rows),
    }
    manifest = StationTelemetryManifestV1(
        schema_version=1,
        artifact_type="commute_help_sumo_station_crossing_telemetry",
        artifact_status="complete",
        evidence_level="modeled_uncalibrated",
        calibration_status="not_calibrated",
        generated_at=datetime.now(UTC),
        producer_name="regional_sumo_station_crossing_telemetry",
        producer_version=STATION_TELEMETRY_PRODUCER_VERSION,
        observer_algorithm=STATION_OBSERVER_ALGORITHM,
        observer_implementation=STATION_OBSERVER_IMPLEMENTATION,
        application_run_id=application_run_id,
        run_identity=run_identity,
        graph_version=graph["graph_version"],
        network_version=network["network_version"],
        sumo_version=network["sumo_version"],
        simulation_mode="mesoscopic",
        seed=seed,
        demand_version=demand_manifest["demand_version"],
        demand_routes_sha256=demand_manifest["artifacts"]["routes"]["sha256"],
        policy_name=POLICY_NAME,
        policy_version=POLICY_VERSION,
        policy_content_digest=plan["policy_content_digest"],
        observation_plan_version=STATION_OBSERVATION_PLAN_VERSION,
        observation_plan_digest=plan["observation_plan_digest"],
        station_count=356,
        interval_seconds=900,
        interval_semantics="half_open_start_inclusive_end_exclusive",
        simulation_start_seconds=0,
        simulation_end_seconds=simulation_end_seconds,
        complete_interval_count_per_variant=complete_intervals,
        omitted_partial_seconds_at_end=simulation_end_seconds % 900,
        variants=("baseline", "scenario"),
        direct_departure_policy="native_departure_child_or_station_local_incomplete",
        departure_provenance_version=DEPARTURE_FRAGMENT_VERSION,
        speed_semantics="not_collected_point_speed_ineligible",
        output={
            "relative_path": STATION_TELEMETRY_PARQUET,
            "sha256": _sha256(output_path),
            "byte_count": output_path.stat().st_size,
            "row_count": len(output_rows),
        },
        row_content_sha256=row_digest,
        content_digest=hashlib.sha256(canonical_json(content).encode()).hexdigest(),
    )
    (pending / STATION_TELEMETRY_MANIFEST).write_text(
        manifest.model_dump_json(indent=2) + "\n"
    )
    if recovery_provenance is not None:
        recovery_payload = {
            **recovery_provenance,
            "station_telemetry_content_digest": manifest.content_digest,
            "station_telemetry_output_sha256": manifest.output.sha256,
            "station_telemetry_row_count": manifest.output.row_count,
        }
        recovery_payload.pop("content_digest", None)
        recovery_payload["content_digest"] = hashlib.sha256(
            canonical_json(
                {
                    key: value
                    for key, value in recovery_payload.items()
                    if key not in {"generated_at", "content_digest"}
                }
            ).encode()
        ).hexdigest()
        recovery = StationTelemetryRecoveryManifestV1.model_validate(recovery_payload)
        (pending / STATION_TELEMETRY_RECOVERY).write_text(
            recovery.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    if final.exists():
        shutil.rmtree(final)
    os.replace(pending, final)
    return manifest


def _network_predecessors(network_path: Path, targets: set[str]) -> set[str]:
    result: set[str] = set()
    for _, element in ET.iterparse(network_path, events=("end",)):
        if (
            element.tag.rsplit("}", 1)[-1] == "connection"
            and element.attrib.get("to") in targets
            and element.attrib.get("from")
        ):
            result.add(str(element.attrib["from"]))
        element.clear()
    return result


def _validate_station_definition(station: ProductionStationDefinition) -> None:
    if station.cross_section_type == "within_edge_position":
        if station.sumo_edge_id is None or station.position_m is None:
            raise StationTelemetryError("Within-edge station lacks edge/position")
    elif not station.transition_from_edge_id or not station.transition_to_edge_id:
        raise StationTelemetryError("Transition station lacks directed pair")


def _zero_metrics() -> dict[str, int]:
    return {
        "subscription_setup_calls": 0,
        "subscription_result_calls": 0,
        "subscription_result_rows": 0,
        "relevant_vehicle_observations": 0,
        "peak_relevant_state_count": 0,
        "crossing_event_count": 0,
        "unresolved_direct_departure_count": 0,
        "resolved_direct_departure_count": 0,
        "relevant_direct_departure_count": 0,
    }


def _row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if row["variant"] == "baseline" else 1,
        int(row["interval_start_seconds"]),
        str(row["station_id"]),
    )


def _arrow_schema() -> pa.Schema:
    fields = []
    for name, annotation in StationTelemetryRecordV1.__annotations__.items():
        if name in {
            "schema_version",
            "seed",
            "interval_start_seconds",
            "interval_end_seconds",
            "interval_duration_seconds",
            "crossing_count",
            "contributing_vehicle_count",
            "resolved_direct_departure_count",
            "unresolved_direct_departure_count",
            "crossing_step_speed_sample_count",
        }:
            dtype = pa.int64()
        elif name == "flow_measurement_complete":
            dtype = pa.bool_()
        elif name in {
            "flow_vph",
            "crossing_step_speed_mean_mps",
            "crossing_step_speed_median_mps",
        }:
            dtype = pa.float64()
        else:
            dtype = pa.string()
        fields.append(pa.field(name, dtype, nullable="None" in str(annotation)))
    return pa.schema(
        fields, metadata={b"producer": b"regional_sumo_station_crossing_telemetry"}
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    stations = plan["stations"]
    within = [
        row for row in stations if row["cross_section_type"] == "within_edge_position"
    ]
    edge_counts = Counter(row["sumo_edge_id"] for row in within)
    transition_pairs = {
        (row["transition_from_edge_id"], row["transition_to_edge_id"])
        for row in stations
        if row["cross_section_type"] == "edge_transition"
    }
    return {
        "accepted_station_count": len(stations),
        "distinct_station_edge_count": len(edge_counts),
        "distinct_transition_pair_count": len(transition_pairs),
        "edges_with_multiple_stations": sum(
            value > 1 for value in edge_counts.values()
        ),
        "maximum_stations_on_one_edge": max(edge_counts.values()),
        "stations_per_edge_distribution": dict(
            sorted(Counter(edge_counts.values()).items())
        ),
        "direction_counts": dict(
            sorted(Counter(row["direction"] for row in stations).items())
        ),
        "observation_plan_digest": plan["observation_plan_digest"],
    }
