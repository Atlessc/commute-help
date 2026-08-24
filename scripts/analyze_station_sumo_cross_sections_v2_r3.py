"""Build station-cross-sections-v2-r3 from frozen V1 and dependency-local geometry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.station_cross_section_v2_r3_service import (
    build_station_cross_sections_v2_r3,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-directory", type=Path, required=True)
    parser.add_argument("--station-mapping", type=Path, required=True)
    parser.add_argument("--comparison-validation", type=Path, required=True)
    parser.add_argument("--synthetic-geometry", type=Path, required=True)
    parser.add_argument("--synthetic-geometry-validation-summary", type=Path, required=True)
    parser.add_argument("--abc-diagnostic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    network = args.network_directory.resolve()
    manifest = build_station_cross_sections_v2_r3(
        parent_directory=network / "station-cross-sections-v1",
        rejected_candidate_directory=network / "station-cross-sections-v2",
        comparison_v1_directory=network / "comparison-relations-v1",
        comparison_v2_directory=network / "comparison-relations-v2",
        comparison_validation_directory=args.comparison_validation.resolve(),
        station_mapping_path=args.station_mapping.resolve(),
        edge_map_path=network / "edge-map.parquet",
        synthetic_geometry_directory=args.synthetic_geometry.resolve(),
        synthetic_geometry_validation_summary_path=args.synthetic_geometry_validation_summary.resolve(),
        abc_diagnostic_directory=args.abc_diagnostic.resolve(),
        output_directory=args.output.resolve(),
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
