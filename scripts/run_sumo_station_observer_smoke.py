"""Run a bounded Phase 2.2e regional observer smoke from an accepted request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from backend.app.services.portal_calibration_v2_importer import canonical_json

POLICY_DIGEST = "5a5506a26997be4fbebbec8a11569e62a13744ddb13e3ec9e861dc356a349ef2"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, choices=(100, 900), default=900)
    parser.add_argument("--observer", action="store_true")
    args = parser.parse_args()

    source = json.loads(args.source_request.read_text(encoding="utf-8"))
    payload = dict(source["request"])
    payload["warmup_minutes"] = 0
    payload["analysis_minutes"] = 15
    payload["edge_telemetry_interval_seconds"] = 900
    if args.observer:
        payload["station_telemetry_interval_seconds"] = 900
        payload["station_cross_section_policy_digest"] = POLICY_DIGEST
    else:
        payload["station_telemetry_interval_seconds"] = None
        payload["station_cross_section_policy_digest"] = None

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source["request"] = payload
    source["application_run_id"] = output.name
    identity = {
        key: value
        for key, value in source.items()
        if key not in {"application_run_id", "run_identity"}
    }
    source["run_identity"] = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    source["station_cross_section_policy_path"] = str(
        Path("data/sumo/networks/active/station-cross-section-policy-v1").resolve()
    )
    request_path = output / "request.json"
    request_path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ)
    if args.duration_seconds < 900:
        env["COMMUTE_HELP_SUMO_PROFILE_SECONDS"] = str(args.duration_seconds)
    completed = subprocess.run(
        [sys.executable, "-m", "backend.app.workers.sumo_worker", "--request", str(request_path)],
        cwd=Path.cwd(),
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
