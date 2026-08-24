"""Generate immutable station-cross-sections-v2 from frozen structural inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.station_cross_section_v2_service import (
    build_station_cross_sections_v2,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network-directory",
        type=Path,
        default=Path("data/sumo/networks/pv-sumo-2026-08-08-v1"),
    )
    parser.add_argument(
        "--station-mappings",
        type=Path,
        default=Path(
            "data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/"
            "station-matching/station-edge-matches.csv"
        ),
    )
    parser.add_argument(
        "--detector-metadata",
        type=Path,
        default=Path(
            "data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/"
            "metadata/detectors.json"
        ),
    )
    parser.add_argument(
        "--graph-edges", type=Path, default=Path("data/graphs/edges.parquet")
    )
    parser.add_argument(
        "--graph-manifest", type=Path, default=Path("data/graphs/graph-manifest.json")
    )
    parser.add_argument(
        "--comparison-validation",
        type=Path,
        default=Path(
            "/home/tyler/CODEX/manual-runs/results/"
            "HIST-MOSS-COMPARISON-RELATIONS-V2-STATIC-VALIDATION-V1"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    network = args.network_directory.resolve()
    output = (
        args.output.resolve()
        if args.output
        else network / "station-cross-sections-v2"
    )
    manifest = build_station_cross_sections_v2(
        parent_directory=network / "station-cross-sections-v1",
        relation_directory=network / "comparison-relations-v2",
        station_mapping_path=args.station_mappings.resolve(),
        detector_metadata_path=args.detector_metadata.resolve(),
        edge_map_path=network / "edge-map.parquet",
        graph_edges_path=args.graph_edges.resolve(),
        graph_manifest_path=args.graph_manifest.resolve(),
        sumo_network_path=network / "metro.net.xml",
        sumo_network_manifest_path=network / "network-manifest.json",
        comparison_validation_directory=args.comparison_validation.resolve(),
        output_directory=output,
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
