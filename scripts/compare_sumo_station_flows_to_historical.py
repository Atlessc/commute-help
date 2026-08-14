"""Build the Phase 2.2g station-cross-section PORTAL to SUMO comparator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.historical_comparison_v2_service import (
    compare_sumo_station_flows_to_historical,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/portal-raw-v1"),
    )
    parser.add_argument(
        "--campaign-manifest",
        type=Path,
        default=Path(
            "data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json"
        ),
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
        "--station-policy",
        type=Path,
        default=Path("data/sumo/networks/active/station-cross-section-policy-v1"),
    )
    parser.add_argument(
        "--graph-edges", type=Path, default=Path("data/graphs/edges.parquet")
    )
    parser.add_argument(
        "--comparator-v1-summary",
        type=Path,
        default=Path(
            "data/sumo/runs/886b4177-8250-44cf-8d7e-cf19dda6deb4/historical-comparison-v1/comparison-summary.json"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = compare_sumo_station_flows_to_historical(
        run_directory=args.run,
        corpus_root=args.corpus,
        campaign_manifest_path=args.campaign_manifest,
        historical_directory=args.historical,
        quality_policy_directory=args.quality_policy,
        station_policy_directory=args.station_policy,
        graph_edges_path=args.graph_edges,
        output_directory=args.output,
        comparator_v1_summary_path=args.comparator_v1_summary,
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
