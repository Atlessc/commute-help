"""Manage private-safe local benchmark trips and score their free-flow floors."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from backend.app.core.settings import get_settings
from backend.app.db.database import DatabaseManager
from backend.app.schemas.benchmarks import BenchmarkTripInput
from backend.app.services.benchmark_service import BenchmarkConflictError, BenchmarkService
from backend.app.services.free_flow_validation_service import FreeFlowValidationService
from backend.app.services.graph_service import GraphService
from backend.app.services.traffic_schedule_service import day_type_for


LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Scenario departure time has no timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed-from-scenario")
    seed.add_argument("--scenario-name", required=True)
    seed.add_argument("--benchmark-id", required=True)
    seed.add_argument("--corridor-label", required=True)
    seed.add_argument("--actual-seconds", type=float, required=True)
    seed.add_argument("--reported-no-traffic-seconds", type=float)
    seed.add_argument("--incident", action="store_true")
    seed.add_argument("--weather-category")
    export = commands.add_parser("export")
    export.add_argument(
        "--output", type=Path, default=Path("data/benchmarks/private/benchmarks.json")
    )
    import_command = commands.add_parser("import")
    import_command.add_argument("path", type=Path)
    score = commands.add_parser("score")
    score.add_argument(
        "--output", type=Path, default=Path("data/benchmarks/private/free-flow-validation")
    )
    commands.add_parser("list")
    args = parser.parse_args()

    started = monotonic()

    def log(message: str) -> None:
        print(
            f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] {message}",
            flush=True,
        )

    settings = get_settings()
    database = DatabaseManager(settings.database_path)
    database.initialize()
    graph = GraphService(settings.graph_path, settings.graph_manifest_path)
    log("LOAD checksum-verified active graph")
    graph.load()
    graph.require_manifest()
    service = BenchmarkService(database, graph)

    if args.command == "seed-from-scenario":
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT content_json FROM scenarios WHERE name=? AND archived=0",
                (args.scenario_name,),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("Scenario name must identify exactly one active scenario")
        content = json.loads(rows[0]["content_json"])
        departure = _timestamp(content["departure_time"])
        value = BenchmarkTripInput(
            id=args.benchmark_id,
            departure_time=departure,
            day_type=day_type_for(departure.astimezone(LOCAL_TIMEZONE)),
            origin_node=str(content["origin"]["node_id"]),
            destination_node=str(content["destination"]["node_id"]),
            corridor_label=args.corridor_label,
            actual_travel_seconds=args.actual_seconds,
            reported_no_traffic_seconds=args.reported_no_traffic_seconds,
            incident_flag=args.incident,
            weather_category=args.weather_category,
        )
        try:
            record = service.create(value)
            log(f"CREATED benchmark={record.id} day_type={record.day_type}")
        except BenchmarkConflictError:
            log(f"SKIP benchmark={value.id} already exists")
        return 0

    if args.command == "export":
        payload = service.export_json(args.output)
        log(f"EXPORTED benchmarks={len(payload['benchmarks'])} path={args.output}")
        return 0

    if args.command == "import":
        records = service.import_json(args.path)
        log(f"IMPORTED benchmarks={len(records)}")
        return 0

    if args.command == "list":
        records = service.list()
        for record in records:
            log(
                f"BENCHMARK id={record.id} corridor={record.corridor_label} "
                f"day_type={record.day_type} actual_seconds={record.actual_travel_seconds:.1f}"
            )
        log(f"COMPLETE count={len(records)}")
        return 0

    records = service.list()
    validator = FreeFlowValidationService(graph)
    validations = []
    for record in records:
        validations.append(validator.validate(record).model_dump(mode="json"))
        if record.reported_no_traffic_seconds is not None:
            validations.append(
                validator.validate(
                    record,
                    record.reported_no_traffic_seconds,
                    value_kind="reported_no_traffic",
                ).model_dump(mode="json")
            )
    args.output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "graph_version": graph.require_manifest().graph_version,
        "benchmark_count": len(records),
        "validation_count": len(validations),
        "all_valid": all(item["valid"] for item in validations),
        "validations": validations,
    }
    report_path = args.output / "validation-report.json"
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (args.output / "validation-report.md").write_text(
        "\n".join(
            [
                "# Benchmark free-flow validation",
                "",
                f"- Graph: `{payload['graph_version']}`",
                f"- Benchmarks: {len(records)}",
                f"- Values checked: {len(validations)}",
                f"- Gate: **{'PASS' if payload['all_valid'] else 'FAIL'}**",
                "",
                *[
                    f"- `{item['benchmark_id']}` {item['value_kind']}: "
                    f"value {item['observed_or_simulated_seconds']:.1f}s; "
                    f"floor {item['network_free_flow_seconds']:.1f}s; "
                    f"minimum {item['minimum_allowed_seconds']:.1f}s; "
                    f"{'PASS' if item['valid'] else 'FAIL'}"
                    for item in validations
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(f"SCORED benchmarks={len(records)} values={len(validations)} all_valid={payload['all_valid']}")
    return 0 if payload["all_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
