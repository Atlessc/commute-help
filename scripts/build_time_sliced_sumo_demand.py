"""Build Phase 3.2 bucket-contained SUMO demand from a Phase 3.1 artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.time_sliced_demand_service import (
    build_time_sliced_demand,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weekday")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    manifest = build_time_sliced_demand(
        source_artifact_path=args.source,
        output_directory=args.output,
        weekday=args.weekday,
        seed_override=args.seed,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "content_digest": manifest.content_digest,
                "demand_sha256": manifest.output.sha256,
                "weekday": manifest.weekday,
                "bucket_count": len(manifest.bucket_audits),
                "generated_vehicle_count": manifest.generated_vehicle_count,
                "target_vehicle_trips": manifest.rounding_audit.target_vehicle_trips,
                "rounding_error_vehicle_trips": (
                    manifest.rounding_audit.aggregate_rounding_error_vehicle_trips
                ),
                "bucket_leakage_count": manifest.departure_audit.leakage_count,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
