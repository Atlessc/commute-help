"""Focused Phase 4 benchmark and physical-floor behavior."""

from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import networkx as nx
import pytest

from backend.app.schemas.benchmarks import BenchmarkTripInput, BenchmarkTripRecord
from backend.app.db.database import DatabaseManager
from backend.app.services.benchmark_service import (
    BenchmarkConflictError,
    BenchmarkImportError,
    BenchmarkService,
)
from backend.app.services.free_flow_validation_service import FreeFlowValidationService
from backend.app.services.planning_time_service import plan_arrive_by, plan_depart_at


def _benchmark() -> BenchmarkTripRecord:
    now = datetime(2026, 8, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
    return BenchmarkTripRecord(
        id="fixture-trip-v1",
        graph_version="fixture-v1",
        departure_time=now,
        day_type="saturday",
        origin_node="1",
        destination_node="3",
        corridor_label="fixture corridor",
        actual_travel_seconds=150,
        reported_no_traffic_seconds=105,
        created_at=now,
        updated_at=now,
    )


def test_free_flow_floor_rejects_impossibly_fast_result() -> None:
    graph = nx.MultiDiGraph()
    graph.add_edge(1, 2, free_flow_seconds=50.0, length_m=500.0)
    graph.add_edge(2, 3, free_flow_seconds=50.0, length_m=500.0)
    graph_service = SimpleNamespace(
        graph=graph,
        node_lookup={"1": 1, "3": 3},
        require_manifest=lambda: SimpleNamespace(graph_version="fixture-v1"),
    )
    validator = FreeFlowValidationService(graph_service)

    assert validator.validate(_benchmark()).valid is True
    invalid = validator.validate(_benchmark(), 80, value_kind="simulated")
    assert invalid.valid is False
    assert invalid.network_free_flow_seconds == 100
    assert invalid.minimum_allowed_seconds == 95
    assert invalid.violation_seconds == 15


def test_depart_at_and_arrive_by_use_time_dependent_duration() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    threshold = datetime(2026, 8, 3, 7, 0, tzinfo=zone)

    def duration(departure: datetime) -> float:
        return 600 if departure < threshold else 1200

    depart_at = plan_depart_at(threshold, duration)
    assert depart_at.arrival_time == datetime(2026, 8, 3, 7, 20, tzinfo=zone)

    arrive_by = plan_arrive_by(
        datetime(2026, 8, 3, 7, 30, tzinfo=zone), duration
    )
    assert arrive_by.departure_time.timestamp() == pytest.approx(
        datetime(2026, 8, 3, 7, 10, tzinfo=zone).timestamp(), abs=1
    )
    assert arrive_by.arrival_time.timestamp() <= datetime(
        2026, 8, 3, 7, 30, tzinfo=zone
    ).timestamp()


def test_benchmark_import_is_private_and_atomic(tmp_path) -> None:
    database = DatabaseManager(tmp_path / "app.db")
    database.initialize()
    graph_service = SimpleNamespace(
        node_lookup={"1": 1, "3": 3},
        require_manifest=lambda: SimpleNamespace(graph_version="fixture-v1"),
    )
    service = BenchmarkService(database, graph_service)
    existing = BenchmarkTripInput.model_validate(
        _benchmark().model_dump(
            exclude={"graph_version", "created_at", "updated_at"}
        )
    )
    service.create(existing)

    new_record = {**existing.model_dump(mode="json"), "id": "new-trip-v1"}
    conflict_record = {**existing.model_dump(mode="json")}
    import_path = tmp_path / "benchmarks.json"
    import_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "graph_version": "fixture-v1",
                "benchmarks": [new_record, conflict_record],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkConflictError):
        service.import_json(import_path)
    assert [record.id for record in service.list()] == ["fixture-trip-v1"]

    unsafe_path = tmp_path / "unsafe.json"
    unsafe_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "graph_version": "fixture-v1",
                "benchmarks": [{**new_record, "coordinates": [-122.0, 45.0]}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkImportError, match="forbidden location fields"):
        service.import_json(unsafe_path)
