"""Print the cheap bounded acceptance summary for a completed Phase 2.2 comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.schemas.sumo_historical_comparison import (
    SumoHistoricalComparisonManifestV1,
)
from backend.app.services.sumo.historical_comparison_service import (
    COMPARISON_DIRECTORY,
    COMPARISON_MANIFEST,
)


def inspect_comparison(run_or_output: Path) -> dict[str, object]:
    output = (
        run_or_output / COMPARISON_DIRECTORY
        if (run_or_output / COMPARISON_DIRECTORY).is_dir()
        else run_or_output
    )
    manifest = SumoHistoricalComparisonManifestV1.model_validate_json(
        (output / COMPARISON_MANIFEST).read_text(encoding="utf-8")
    )
    summary = json.loads((output / manifest.summary_output.relative_path).read_text(encoding="utf-8"))
    return {
        "compared_row_count": manifest.comparison_output.row_count,
        "mapping_coverage": summary["coverage"],
        "primary_direct_evidence": summary["primary_metrics"],
        "slowdown_residual_breakdowns": {
            key: summary["breakdowns"][key]
            for key in ("variant", "weekday", "bucket", "direction", "corridor")
        },
        "worst_25_edge_time_residuals": summary["worst_25_direct_evidence_residuals"],
        "identity": {
            "application_run_id": manifest.application_run_id,
            "run_identity": manifest.run_identity,
            "content_digest": manifest.content_digest,
            "evidence_level": manifest.evidence_level,
            "calibration_status": manifest.calibration_status,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_comparison(args.run.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
