"""Build deterministic SUMO demand from an assignment-ready regional OD intake."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from backend.app.core.settings import get_settings
from backend.app.services.sumo.demand_service import SumoDemandError, build_sumo_demand


ROOT = Path(__file__).resolve().parents[1]


class DemandLogger:
    def __init__(self) -> None:
        self.started = monotonic()
        self.lines: list[str] = []

    def __call__(self, message: str, level: str = "INFO") -> None:
        elapsed = monotonic() - self.started
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        line = f"[{timestamp}] [+{elapsed:0.3f}s] {level} {message}"
        self.lines.append(line)
        print(line, flush=True)


def _parse_clock(value: str) -> int:
    pieces = value.split(":")
    if len(pieces) != 2:
        raise argparse.ArgumentTypeError("start time must be HH:MM")
    try:
        hour, minute = (int(piece) for piece in pieces)
    except ValueError as error:
        raise argparse.ArgumentTypeError("start time must be HH:MM") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("start time must be a valid 24-hour clock time")
    return hour * 3600 + minute * 60


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--demand-version", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--start-time", type=_parse_clock, required=True)
    parser.add_argument("--scale", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=20260808)
    parser.add_argument("--connectors-per-zone", type=int, default=4)
    parser.add_argument("--max-connector-distance-m", type=float, default=5_000.0)
    parser.add_argument("--network", type=Path, default=settings.sumo_network_manifest_path.parent / "metro.net.xml")
    parser.add_argument("--network-manifest", type=Path, default=settings.sumo_network_manifest_path)
    parser.add_argument("--edge-map", type=Path, default=settings.sumo_network_manifest_path.parent / "edge-map.parquet")
    parser.add_argument("--duarouter", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    output = (
        args.output.resolve()
        if args.output is not None
        else (settings.sumo_demand_path / args.demand_version).resolve()
    )
    logger = DemandLogger()
    logger(
        f"START demand={args.demand_version} period={args.period} "
        f"scale={args.scale:g} seed={args.seed}"
    )
    try:
        build_sumo_demand(
            intake_directory=args.intake.resolve(),
            network_path=args.network.resolve(),
            network_manifest_path=args.network_manifest.resolve(),
            edge_map_path=args.edge_map.resolve(),
            output_directory=output,
            demand_version=args.demand_version,
            period=args.period,
            start_seconds=args.start_time,
            sampling_scale=args.scale,
            seed=args.seed,
            connectors_per_zone=args.connectors_per_zone,
            max_connector_distance_m=args.max_connector_distance_m,
            duarouter_binary=args.duarouter,
            log=logger,
        )
        logger(f"PASS artifacts={output}")
        (output / "build.log").write_text("\n".join(logger.lines) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError, SumoDemandError) as error:
        logger(f"FAILED {error}", "ERROR")
        if output.exists():
            (output / "build.log").write_text(
                "\n".join(logger.lines) + "\n", encoding="utf-8"
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
