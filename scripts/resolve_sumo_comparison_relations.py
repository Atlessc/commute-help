"""Build the immutable Phase 2.2a accepted relation-resolution analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.comparison_relation_service import (
    resolve_comparison_relations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network-directory",
        type=Path,
        default=Path("data/sumo/networks/pv-sumo-2026-08-08-v1"),
    )
    parser.add_argument(
        "--historical-directory",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1"),
    )
    parser.add_argument(
        "--policy-directory",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1"),
    )
    parser.add_argument(
        "--graph-edges",
        type=Path,
        default=Path("data/graphs/edges.parquet"),
    )
    parser.add_argument(
        "--station-mappings",
        type=Path,
        default=Path("data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-edge-matches.csv"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    network = args.network_directory.resolve()
    output = (
        args.output.resolve()
        if args.output
        else network / "comparison-relations-v1"
    )
    manifest = resolve_comparison_relations(
        edge_map_path=network / "edge-map.parquet",
        edge_map_report_path=network / "edge-map-report.json",
        sumo_network_path=network / "metro.net.xml",
        network_manifest_path=network / "network-manifest.json",
        graph_edges_path=args.graph_edges.resolve(),
        historical_directory=args.historical_directory.resolve(),
        policy_directory=args.policy_directory.resolve(),
        station_mapping_path=args.station_mappings.resolve(),
        output_directory=output,
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
