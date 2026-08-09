"""Build the normal-network proxy OD and background-flow seed artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from backend.app.services.background_seed_service import (
    BackgroundSeedConfig,
    build_background_seed,
    report_markdown,
)


ROOT = Path(__file__).resolve().parents[1]


class BuildLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str, level: str = "INFO") -> None:
        elapsed = time.monotonic() - self.started
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        line = (
            f"[{timestamp}] [+{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}] "
            f"{level} {message}"
        )
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _atomic_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build conserved proxy OD demand and held-out-validated edge-flow seeds."
    )
    parser.add_argument(
        "--edge-priors",
        type=Path,
        default=ROOT
        / "data/traffic/processed/edge-priors/2026-07-31-portland-vancouver-v1/edge-priors.parquet",
    )
    parser.add_argument("--nodes", type=Path, default=ROOT / "data/graphs/nodes.parquet")
    parser.add_argument(
        "--graph-manifest", type=Path, default=ROOT / "data/graphs/graph-manifest.json"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--zone-count", type=int, default=80)
    parser.add_argument("--maximum-od-pairs", type=int, default=6000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    graph_version = str(json.loads(args.graph_manifest.read_text())["graph_version"])
    output = args.output or (
        ROOT / "data/traffic/processed/background-seeds" / graph_version
    )
    output.mkdir(parents=True, exist_ok=True)
    logger = BuildLogger(output / "build.log")
    logger(f"START background-seed build graph={graph_version}")
    try:
        edge_flows, od_pairs, report = build_background_seed(
            edge_priors_path=args.edge_priors,
            nodes_path=args.nodes,
            graph_version=graph_version,
            config=BackgroundSeedConfig(
                zone_count=args.zone_count,
                maximum_od_pairs=args.maximum_od_pairs,
            ),
            progress=logger,
        )
        edge_part = output / "edge-flow-seeds.parquet.part"
        edge_flows.to_parquet(edge_part, index=False)
        edge_part.replace(output / "edge-flow-seeds.parquet")
        od_part = output / "od-demand-seeds.parquet.part"
        od_pairs.to_parquet(od_part, index=False)
        od_part.replace(output / "od-demand-seeds.parquet")
        _atomic_text(output / "validation-report.json", json.dumps(report, indent=2) + "\n")
        _atomic_text(output / "validation-report.md", report_markdown(report))
        logger(
            f"COMPLETE status={report['status']} wrote {len(edge_flows):,} edges and "
            f"{len(od_pairs):,} OD paths"
        )
        return 0
    except Exception as error:
        logger(f"FAILED {error}", "ERROR")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
