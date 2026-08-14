"""Characterize Phase 1.3 candidate evidence without selecting quality policy."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.historical_calibration_v2_quality_service import (
    characterize_historical_calibration_v2_quality,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edge-partitions", type=int, default=48)
    parser.add_argument("--batch-size", type=int, default=131_072)
    args = parser.parse_args()
    manifest = characterize_historical_calibration_v2_quality(
        source_directory=args.source,
        output_directory=args.output,
        edge_partition_count=args.edge_partitions,
        batch_size=args.batch_size,
    )
    print(
        "quality characterization complete: "
        f"{manifest.output_profile_characterization.row_count:,} profiles, "
        f"digest={manifest.content_digest}"
    )


if __name__ == "__main__":
    main()
