"""Apply Phase 1.3 quality policy-v1 to candidate characterization evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.historical_calibration_v2_policy_service import (
    apply_historical_calibration_v2_quality_policy,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--characterization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = apply_historical_calibration_v2_quality_policy(
        candidate_directory=args.candidate,
        characterization_directory=args.characterization,
        output_directory=args.output,
    )
    print(
        "historical quality policy applied: "
        f"{manifest.output_profile_status.row_count:,} profiles, "
        f"digest={manifest.content_digest}"
    )


if __name__ == "__main__":
    main()
