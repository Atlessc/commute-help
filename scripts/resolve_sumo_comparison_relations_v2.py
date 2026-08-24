"""Generate immutable comparison-relations-v2 from frozen structural lineage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.comparison_relation_boundary_repair_service import (
    build_comparison_relations_v2,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network-directory",
        type=Path,
        default=Path("data/sumo/networks/pv-sumo-2026-08-08-v1"),
    )
    parser.add_argument(
        "--graph-edges", type=Path, default=Path("data/graphs/edges.parquet")
    )
    parser.add_argument(
        "--diagnostic-directory",
        type=Path,
        default=Path(
            "/home/tyler/CODEX/manual-runs/results/"
            "HIST-MOSS-R3-STATIC-TRANSITION-DISAGREEMENTS-DIAGNOSTIC-V2"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    network = args.network_directory.resolve()
    diagnostic = args.diagnostic_directory.resolve()
    output = (
        args.output.resolve()
        if args.output
        else network / "comparison-relations-v2"
    )
    manifest = build_comparison_relations_v2(
        parent_directory=network / "comparison-relations-v1",
        edge_map_path=network / "edge-map.parquet",
        sumo_network_path=network / "metro.net.xml",
        network_manifest_path=network / "network-manifest.json",
        graph_edges_path=args.graph_edges.resolve(),
        diagnostic_summary_path=diagnostic / "summary.json",
        diagnostic_provenance_path=diagnostic / "provenance.json",
        output_directory=output,
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
