"""Build one bounded Phase 2.2 historical-to-SUMO diagnostic comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.historical_comparison_service import (
    compare_sumo_to_historical,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "--historical",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1"),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1"),
    )
    parser.add_argument(
        "--edge-map", type=Path, default=Path("data/sumo/networks/active/edge-map.parquet")
    )
    parser.add_argument(
        "--edge-map-report",
        type=Path,
        default=Path("data/sumo/networks/pv-sumo-2026-08-08-v1/edge-map-report.json"),
    )
    parser.add_argument(
        "--graph-edges", type=Path, default=Path("data/graphs/edges.parquet")
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = compare_sumo_to_historical(
        run_directory=args.run,
        historical_directory=args.historical,
        policy_directory=args.policy,
        edge_map_path=args.edge_map,
        edge_map_report_path=args.edge_map_report,
        graph_edges_path=args.graph_edges,
        output_directory=args.output,
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
