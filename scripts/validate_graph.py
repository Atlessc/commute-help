"""Re-run integrity checks against an existing GraphML artifact."""

import argparse
import json
from pathlib import Path

import osmnx as ox

from backend.app.services.graph_build_service import (
    load_region,
    refresh_manifest_validation,
    validate_graph,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a generated routing graph.")
    parser.add_argument(
        "--graph",
        type=Path,
        default=Path("data/graphs/portland-vancouver.graphml"),
    )
    parser.add_argument(
        "--region",
        type=Path,
        default=Path("data/regions/portland-vancouver-v1.geojson"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/graphs/validation-report.json"),
    )
    args = parser.parse_args()

    graph = ox.io.load_graphml(args.graph)
    graph_version = str(graph.graph.get("graph_version", "unknown"))
    report = validate_graph(graph, load_region(args.region), graph_version)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = args.graph.parent / "graph-manifest.json"
    if manifest_path.exists():
        refresh_manifest_validation(report, args.report, manifest_path)
    print(f"Validation gate: {'PASS' if report['gate_passed'] else 'FAIL'}")
    print(f"Report: {args.report}")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
