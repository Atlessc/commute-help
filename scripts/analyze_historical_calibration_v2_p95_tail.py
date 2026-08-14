"""Build the Phase 1.4 legacy-P95 versus modern edge/time analysis."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.historical_calibration_v2_p95_tail_service import (
    analyze_historical_calibration_v2_p95_tail,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-campaign", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--characterization", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--graph-edges", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    manifest = analyze_historical_calibration_v2_p95_tail(
        legacy_campaign_directory=args.legacy_campaign,
        candidate_directory=args.candidate,
        characterization_directory=args.characterization,
        policy_directory=args.policy,
        graph_edges_path=args.graph_edges,
        output_directory=args.output,
    )
    print(
        "P95 tail analysis complete: "
        f"{manifest.output_profiles.row_count:,} PM profiles, "
        f"digest={manifest.content_digest}, "
        f"elapsed={time.perf_counter() - started:.2f}s"
    )


if __name__ == "__main__":
    main()
