"""Register Comparator-v2 as an unpromoted Phase 2.3 production candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.calibration_promotion_service import (
    register_comparator_v2_production_candidate,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Register immutable Comparator-v2 evidence as a candidate without "
            "inventing production accuracy thresholds."
        )
    )
    parser.add_argument("--comparator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-comparator-digest", required=True)
    args = parser.parse_args()
    decision, manifest = register_comparator_v2_production_candidate(
        comparator_directory=args.comparator,
        output_directory=args.output,
        expected_comparator_digest=args.expected_comparator_digest,
    )
    print(
        json.dumps(
            {
                "candidate_content_digest": decision.candidate.artifact.content_digest,
                "criteria_policy_status": decision.criteria_policy.policy_status,
                "decision_content_digest": decision.content_digest,
                "lifecycle_state": decision.transition.to_state,
                "manifest_content_digest": manifest.content_digest,
                "output": str(args.output.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
