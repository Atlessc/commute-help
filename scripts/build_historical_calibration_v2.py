"""Build Phase 1.3 edge/date and weekday calibration artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.historical_calibration_v2_compiler import (
    compile_historical_calibration_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--campaign-manifest", type=Path, required=True)
    parser.add_argument("--graph-edges", type=Path, required=True)
    parser.add_argument("--graph-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edge-partitions", type=int, default=128)
    parser.add_argument("--json-block-mib", type=int, default=4)
    args = parser.parse_args()
    manifest = compile_historical_calibration_v2(
        corpus_root=args.corpus,
        campaign_manifest_path=args.campaign_manifest,
        graph_edges_path=args.graph_edges,
        graph_version=args.graph_version,
        output_directory=args.output,
        edge_partition_count=args.edge_partitions,
        json_block_size=args.json_block_mib * 1024 * 1024,
    )
    print(
        f"historical calibration compiled: {manifest.date_level.row_count:,} "
        f"date rows, {manifest.weekday_profiles.row_count:,} profile rows, "
        f"digest={manifest.content_digest}"
    )


if __name__ == "__main__":
    main()
