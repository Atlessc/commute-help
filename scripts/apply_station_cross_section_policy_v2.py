"""Generate traffic-blind station-cross-section-policy-v2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.station_cross_section_policy_v2_service import (
    build_station_cross_section_policy_v2,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-directory", type=Path, required=True)
    parser.add_argument("--station-cross-section-validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    network = args.network_directory.resolve()
    manifest = build_station_cross_section_policy_v2(
        parent_policy_directory=network / "station-cross-section-policy-v1",
        parent_cross_section_directory=network / "station-cross-sections-v1",
        source_cross_section_directory=network / "station-cross-sections-v2-r3",
        source_cross_section_validation_summary_path=args.station_cross_section_validation.resolve(),
        relation_directory=network / "comparison-relations-v2",
        output_directory=args.output.resolve(),
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
