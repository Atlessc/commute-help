"""Run the subprocess-isolated Phase 2.2c installed-SUMO feasibility proof."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.station_crossing_counter_service import (
    build_feasibility_artifact,
    run_installed_crossing_feasibility,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/sumo/feasibility/station-crossing-counter-v1"),
    )
    parser.add_argument("--native-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--work-directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    binary = Path(__file__).resolve().parents[1] / ".venv" / "bin"
    if args.native_output:
        if args.work_directory is None:
            parser.error("--native-output requires --work-directory")
        result = run_installed_crossing_feasibility(
            work_directory=args.work_directory,
            netconvert_binary=binary / "netconvert",
            sumo_binary=binary / "sumo",
        )
        args.native_output.write_text(canonical_json(result) + "\n", encoding="utf-8")
        return 0
    with tempfile.TemporaryDirectory(
        prefix="commute-help-crossing-feasibility-"
    ) as raw:
        root = Path(raw)
        native = root / "native.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.analyze_sumo_station_crossing_feasibility",
                "--native-output",
                str(native),
                "--work-directory",
                str(root / "sumo"),
            ],
            check=True,
        )
        result = json.loads(native.read_text(encoding="utf-8"))
    manifest = build_feasibility_artifact(
        feasibility=result, output_directory=args.output.resolve()
    )
    print(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
