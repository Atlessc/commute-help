"""Build the versioned Portland-Vancouver OpenStreetMap routing graph."""

import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.services.graph_build_service import (
    download_drive_graph,
    load_frozen_drive_graph,
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
        "--source",
        type=Path,
        default=None,
        help="Frozen local OSM XML source; avoids all network access.",
    )
    parser.add_argument("--source-version", default=None)
    parser.add_argument("--source-sha256", default=None)
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
    if args.source is not None:
        if not args.source.is_file():
            parser.error(f"Frozen source does not exist: {args.source}")
        if not args.source_version or not args.source_sha256:
            parser.error("--source-version and --source-sha256 are required with --source")
        if _sha256(args.source) != args.source_sha256:
            parser.error("Frozen source checksum does not match --source-sha256")
        print(f"Loading frozen local source {args.source_version}; no network access is used.")
        graph = load_frozen_drive_graph(args.source, region)
    else:
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
        source_label=(
            "Frozen OpenStreetMap XML recovered from complete OSMnx Overpass cache"
            if args.source is not None
            else "OpenStreetMap via OSMnx Overpass download"
        ),
        osm_source_version=args.source_version,
        osm_source_sha256=args.source_sha256,
    )

    print(f"Wrote graph artifacts to {args.output}.")
    print(
        "Validation gate: "
        + ("PASS" if manifest.validation_passed else "FAIL — inspect validation-report.json")
    )
    return 0 if manifest.validation_passed else 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
