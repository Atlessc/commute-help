"""Apply Phase 2.2d station-to-SUMO cross-section policy-v1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.station_cross_section_policy_service import (
    build_station_cross_section_policy,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/sumo/networks/active/station-cross-sections-v1"),
    )
    parser.add_argument(
        "--relations",
        type=Path,
        default=Path("data/sumo/networks/active/comparison-relations-v1"),
    )
    parser.add_argument(
        "--historical",
        type=Path,
        default=Path(
            "data/traffic/processed/calibration-v2/historical-edge-profiles-v1"
        ),
    )
    parser.add_argument(
        "--quality-policy",
        type=Path,
        default=Path(
            "data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/sumo/networks/active/station-cross-section-policy-v1"),
    )
    args = parser.parse_args()
    manifest = build_station_cross_section_policy(
        source_directory=args.source.resolve(),
        relation_directory=args.relations.resolve(),
        historical_directory=args.historical.resolve(),
        quality_policy_directory=args.quality_policy.resolve(),
        output_directory=args.output.resolve(),
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
