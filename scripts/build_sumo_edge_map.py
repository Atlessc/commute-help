"""Build the versioned directed app-edge to SUMO-edge mapping artifacts."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from backend.app.services.sumo.edge_mapping_service import build_edge_map


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-edges", type=Path, required=True)
    parser.add_argument("--app-manifest", type=Path, required=True)
    parser.add_argument("--sumo-network", type=Path, required=True)
    parser.add_argument("--sumo-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.output_dir / "edge-map.log"
    started = monotonic()

    def log(message: str) -> None:
        line = (
            f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] "
            f"{message}"
        )
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    build_edge_map(
        app_edges_path=args.app_edges,
        app_graph_manifest_path=args.app_manifest,
        sumo_network_path=args.sumo_network,
        sumo_network_manifest_path=args.sumo_manifest,
        output_dir=args.output_dir,
        log=log,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
