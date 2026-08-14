"""Finalize station telemetry from complete promoted checkpoints without SUMO."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.services.sumo.station_telemetry_recovery_service import (
    recover_station_telemetry_from_checkpoints,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--expected-run-identity", required=True)
    args = parser.parse_args()
    telemetry, recovery = recover_station_telemetry_from_checkpoints(
        run_directory=args.run,
        expected_run_identity=args.expected_run_identity,
    )
    print(
        json.dumps(
            {
                "station_telemetry": telemetry.model_dump(mode="json"),
                "recovery": recovery.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
