"""Build the versioned Portland-Vancouver OpenStreetMap routing graph."""

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.services.graph_build_service import (
    download_drive_graph,
    load_region,
    normalize_graph,
    validate_graph,
    write_graph_artifacts,
)

DEFAULT_REGION = Path("data/regions/portland-vancouver-v1.geojson")
DEFAULT_OUTPUT = Path("data/graphs")
DEFAULT_CACHE = Path("data/osm/cache")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download, normalize, validate, and save the regional drive graph."
    )
    parser.add_argument("--region", type=Path, default=DEFAULT_REGION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--graph-version",
        default=None,
        help="Version label; defaults to the Pacific build date plus region ID.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing graph artifacts after an intentional rebuild.",
    )
    args = parser.parse_args()

    existing_graph = args.output / "portland-vancouver.graphml"
    if existing_graph.exists() and not args.force:
        parser.error(
            f"{existing_graph} already exists; use --force for an intentional rebuild"
        )

    region = load_region(args.region)
    graph_version = args.graph_version or (
        f"{datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()}-{region.id}"
    )

    print(f"Building {graph_version} from {args.region}…")
    print("This downloads OpenStreetMap road data and may take several minutes.")
    graph = download_drive_graph(region, args.cache)
    print(f"Downloaded {graph.number_of_nodes():,} nodes and {graph.number_of_edges():,} edges.")

    normalize_graph(graph, graph_version)
    report = validate_graph(graph, region, graph_version)
    manifest = write_graph_artifacts(
        graph,
        region,
        graph_version,
        args.output,
        report,
    )

    print(f"Wrote graph artifacts to {args.output}.")
    print(
        "Validation gate: "
        + ("PASS" if manifest.validation_passed else "FAIL — inspect validation-report.json")
    )
    return 0 if manifest.validation_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
