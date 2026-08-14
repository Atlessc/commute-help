"""Run bounded E1 feasibility and build Phase 2.2b station projections."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.station_cross_section_service import (
    build_station_cross_sections,
    run_installed_e1_feasibility,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network-directory",
        type=Path,
        default=Path("data/sumo/networks/pv-sumo-2026-08-08-v1"),
    )
    parser.add_argument(
        "--historical-directory",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1"),
    )
    parser.add_argument(
        "--policy-directory",
        type=Path,
        default=Path("data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1"),
    )
    parser.add_argument(
        "--station-mappings",
        type=Path,
        default=Path("data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-edge-matches.csv"),
    )
    parser.add_argument(
        "--detector-metadata",
        type=Path,
        default=Path("data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/metadata/detectors.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--e1-only-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--e1-work-directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    binary_directory = Path(__file__).resolve().parents[1] / ".venv" / "bin"
    if args.e1_only_output:
        if not args.e1_work_directory:
            parser.error("--e1-only-output requires --e1-work-directory")
        feasibility = run_installed_e1_feasibility(
            work_directory=args.e1_work_directory,
            netconvert_binary=binary_directory / "netconvert",
            sumo_binary=binary_directory / "sumo",
        )
        args.e1_only_output.write_text(
            canonical_json(feasibility) + "\n", encoding="utf-8"
        )
        return 0
    network = args.network_directory.resolve()
    output = (
        args.output.resolve()
        if args.output
        else network / "station-cross-sections-v1"
    )
    with tempfile.TemporaryDirectory(prefix="commute-help-e1-feasibility-") as value:
        work = Path(value)
        feasibility_path = work / "e1-feasibility.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.analyze_station_sumo_cross_sections",
                "--e1-only-output",
                str(feasibility_path),
                "--e1-work-directory",
                str(work / "sumo"),
            ],
            check=True,
        )
        manifest = build_station_cross_sections(
            station_mapping_path=args.station_mappings.resolve(),
            detector_metadata_path=args.detector_metadata.resolve(),
            edge_map_path=network / "edge-map.parquet",
            relation_directory=network / "comparison-relations-v1",
            historical_directory=args.historical_directory.resolve(),
            policy_directory=args.policy_directory.resolve(),
            e1_feasibility_path=feasibility_path,
            output_directory=output,
        )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
