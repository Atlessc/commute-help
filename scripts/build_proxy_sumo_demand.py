"""Build immediately usable local proxy OD demand for an arbitrary local time."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from backend.app.core.settings import get_settings
from backend.app.services.sumo.demand_service import SumoDemandError, build_sumo_demand
from backend.app.services.sumo.proxy_od_service import (
    ProxyOdError,
    compile_proxy_od_snapshot,
)
from backend.app.services.traffic_schedule_service import (
    LOCAL_TIMEZONE,
    TrafficScheduleService,
)


class Logger:
    def __init__(self) -> None:
        self.started = monotonic()
        self.lines: list[str] = []

    def __call__(self, message: str, level: str = "INFO") -> None:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        line = f"[{timestamp}] [+{monotonic() - self.started:0.3f}s] {level} {message}"
        self.lines.append(line)
        print(line, flush=True)


def _departure(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("departure must include a timezone offset")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demand-version", required=True)
    parser.add_argument("--departure", type=_departure, required=True)
    parser.add_argument("--duration-minutes", type=int, default=90)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260808)
    parser.add_argument("--connectors-per-zone", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    graph_manifest = __import__("json").loads(
        settings.graph_manifest_path.read_text(encoding="utf-8")
    )
    graph_version = str(graph_manifest["graph_version"])

    network_manifest = __import__("json").loads(
        settings.sumo_network_manifest_path.read_text(
            encoding="utf-8"
        )
    )

    gateway_connector_path = (
        settings.sumo_path
        / "config/gateway-connectors"
        / f"{network_manifest['network_version']}.json"
    )

    source_directory = (
        settings.traffic_path / "processed/proxy-od" / args.demand_version
    ).resolve()
    output_directory = (settings.sumo_demand_path / args.demand_version).resolve()
    logger = Logger()
    logger(
        f"START proxy_demand={args.demand_version} departure={args.departure.isoformat()} "
        f"duration_minutes={args.duration_minutes} scale={args.scale:g} seed={args.seed}"
    )
    try:
        schedule = TrafficScheduleService(
            settings.traffic_schedule_path,
            settings.traffic_schedule_manifest_path,
        )
        schedule.load()
        compile_proxy_od_snapshot(
            background_seed_directory=settings.traffic_path
            / "processed/background-seeds"
            / graph_version,
            nodes_path=Path("data/graphs/nodes.parquet"),
            graph_manifest_path=settings.graph_manifest_path,
            schedule_service=schedule,
            departure_time=args.departure,
            duration_minutes=args.duration_minutes,
            output_directory=source_directory,
            source_version=args.demand_version,
            log=logger,
        )
        local = args.departure.astimezone(LOCAL_TIMEZONE)
        build_sumo_demand(
            intake_directory=source_directory,
            network_path=settings.sumo_network_manifest_path.parent / "metro.net.xml",
            network_manifest_path=settings.sumo_network_manifest_path,
            edge_map_path=settings.sumo_network_manifest_path.parent / "edge-map.parquet",
            output_directory=output_directory,
            demand_version=args.demand_version,
            period="proxy_snapshot",
            start_seconds=local.hour * 3600 + local.minute * 60 + local.second,
            sampling_scale=args.scale,
            seed=args.seed,
            connectors_per_zone=args.connectors_per_zone,
            gateway_connector_path=gateway_connector_path,
            log=logger,
        )
        logger(f"PASS proxy demand artifacts={output_directory}")
        (output_directory / "build.log").write_text(
            "\n".join(logger.lines) + "\n", encoding="utf-8"
        )
        return 0
    except (OSError, ValueError, ProxyOdError, SumoDemandError) as error:
        logger(f"FAILED {error}", "ERROR")
        for directory in (source_directory, output_directory):
            if directory.exists():
                (directory / "build.log").write_text(
                    "\n".join(logger.lines) + "\n", encoding="utf-8"
                )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
