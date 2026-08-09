"""SQLite-backed private local benchmark trip management."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from backend.app.db.database import DatabaseManager
from backend.app.schemas.benchmarks import BenchmarkTripInput, BenchmarkTripRecord
from backend.app.services.graph_service import GraphService


FORBIDDEN_LOCATION_KEYS = {
    "address",
    "origin_address",
    "destination_address",
    "lat",
    "lng",
    "latitude",
    "longitude",
    "coordinates",
}


class BenchmarkConflictError(RuntimeError):
    pass


class BenchmarkImportError(ValueError):
    pass


class BenchmarkService:
    def __init__(self, database: DatabaseManager, graph_service: GraphService) -> None:
        self.database = database
        self.graph_service = graph_service

    def create(self, value: BenchmarkTripInput) -> BenchmarkTripRecord:
        _assert_private_safe(value.model_dump(mode="json"))
        manifest = self.graph_service.require_manifest()
        self._validate_graph_endpoints(value)
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            try:
                self._insert(connection, value, manifest.graph_version, now)
                connection.commit()
            except sqlite3.IntegrityError as error:
                if "benchmark_trips.id" in str(error):
                    raise BenchmarkConflictError(value.id) from error
                raise
        return BenchmarkTripRecord(
            **value.model_dump(),
            graph_version=manifest.graph_version,
            created_at=now,
            updated_at=now,
        )

    def list(self) -> list[BenchmarkTripRecord]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM benchmark_trips ORDER BY departure_time, id"
            ).fetchall()
        return [_record(row) for row in rows]

    def export_json(self, path: Path) -> dict[str, Any]:
        records = self.list()
        payload = {
            "schema_version": 1,
            "graph_version": self.graph_service.require_manifest().graph_version,
            "privacy_contract": "graph_nodes_or_coarse_zones_no_addresses_or_coordinates",
            "benchmarks": [record.model_dump(mode="json") for record in records],
        }
        _assert_private_safe(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return payload

    def import_json(self, path: Path) -> list[BenchmarkTripRecord]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            _assert_private_safe(payload)
            if not isinstance(payload, dict):
                raise BenchmarkImportError("Benchmark export must be a JSON object")
            if payload.get("schema_version") != 1:
                raise BenchmarkImportError("Unsupported benchmark export schema")
            current_graph = self.graph_service.require_manifest().graph_version
            if payload.get("graph_version") != current_graph:
                raise BenchmarkImportError("Benchmark export graph version is not active")
            values = TypeAdapter(list[BenchmarkTripInput]).validate_python(
                [
                    {
                        key: value
                        for key, value in record.items()
                        if key not in {"graph_version", "created_at", "updated_at"}
                    }
                    for record in payload["benchmarks"]
                ]
            )
        except (OSError, KeyError, json.JSONDecodeError, ValidationError) as error:
            raise BenchmarkImportError(str(error)) from error

        ids = [value.id for value in values]
        if len(ids) != len(set(ids)):
            raise BenchmarkImportError("Benchmark export contains duplicate IDs")
        for value in values:
            self._validate_graph_endpoints(value)

        now = datetime.now(UTC)
        with self.database.connect() as connection:
            existing = {
                row["id"]
                for row in connection.execute("SELECT id FROM benchmark_trips").fetchall()
            }
            conflicts = sorted(existing.intersection(ids))
            if conflicts:
                raise BenchmarkConflictError(", ".join(conflicts))
            try:
                for value in values:
                    self._insert(connection, value, current_graph, now)
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise BenchmarkImportError(str(error)) from error
        return [
            BenchmarkTripRecord(
                **value.model_dump(),
                graph_version=current_graph,
                created_at=now,
                updated_at=now,
            )
            for value in values
        ]

    def _validate_graph_endpoints(self, value: BenchmarkTripInput) -> None:
        if value.origin_node is None:
            return
        if value.origin_node not in self.graph_service.node_lookup:
            raise ValueError("Benchmark origin node is not in the active graph")
        if value.destination_node not in self.graph_service.node_lookup:
            raise ValueError("Benchmark destination node is not in the active graph")

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        value: BenchmarkTripInput,
        graph_version: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO benchmark_trips
            (id, graph_version, departure_time, day_type, origin_node,
             destination_node, origin_zone, destination_zone, corridor_label,
             actual_travel_seconds, reported_no_traffic_seconds,
             incident_flag, weather_category,
             checkpoints_json, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                value.id,
                graph_version,
                value.departure_time.isoformat(),
                value.day_type,
                value.origin_node,
                value.destination_node,
                value.origin_zone,
                value.destination_zone,
                value.corridor_label,
                value.actual_travel_seconds,
                value.reported_no_traffic_seconds,
                int(value.incident_flag),
                value.weather_category,
                json.dumps(value.checkpoints, separators=(",", ":")),
                value.notes,
                now.isoformat(),
                now.isoformat(),
            ),
        )


def _record(row: Any) -> BenchmarkTripRecord:
    return BenchmarkTripRecord(
        id=row["id"],
        graph_version=row["graph_version"],
        departure_time=row["departure_time"],
        day_type=row["day_type"],
        origin_node=row["origin_node"],
        destination_node=row["destination_node"],
        origin_zone=row["origin_zone"],
        destination_zone=row["destination_zone"],
        corridor_label=row["corridor_label"],
        actual_travel_seconds=row["actual_travel_seconds"],
        reported_no_traffic_seconds=row["reported_no_traffic_seconds"],
        incident_flag=bool(row["incident_flag"]),
        weather_category=row["weather_category"],
        checkpoints=json.loads(row["checkpoints_json"]),
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _assert_private_safe(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN_LOCATION_KEYS.intersection(
            str(key).lower() for key in value
        )
        if forbidden:
            raise BenchmarkImportError(
                f"Private benchmark payload contains forbidden location fields: {sorted(forbidden)}"
            )
        for child in value.values():
            _assert_private_safe(child)
    elif isinstance(value, list):
        for child in value:
            _assert_private_safe(child)
