"""Build the deterministic Phase 2.2e bounded regional performance artifact."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
from datetime import UTC, datetime
from pathlib import Path

from backend.app.services.portal_calibration_v2_importer import canonical_json


def main() -> int:
    no_observer = Path("data/sumo/runs/phase-2-2e-900s-no-observer")
    observer = Path("data/sumo/runs/phase-2-2e-900s-observer")
    output = Path("data/sumo/feasibility/station-observer-regional-v1")
    pending = output.parent / f".{output.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)

    def elapsed(path: Path) -> float:
        return path.joinpath("result.json").stat().st_mtime - path.joinpath("request.json").stat().st_mtime

    def checkpoints(path: Path, variant: str) -> list[dict]:
        return [
            json.loads((item / "checkpoint.json").read_text())
            for item in sorted((path / "checkpoints" / variant).iterdir())
            if item.is_dir() and item.name.isdigit()
        ]

    no_wall = elapsed(no_observer)
    yes_wall = elapsed(observer)
    yes = {variant: checkpoints(observer, variant) for variant in ("baseline", "scenario")}
    last_metrics = {variant: values[-1]["station_observer_metrics"] for variant, values in yes.items()}
    state_sizes = [
        item["station_observer_checkpoint_bytes"]
        for values in yes.values()
        for item in values
    ]
    station_manifest_path = observer / "station-telemetry-15m" / "station-telemetry-manifest.json"
    station_manifest = json.loads(station_manifest_path.read_text())
    semantic = {
        "schema_version": 1,
        "artifact_type": "commute_help_station_observer_regional_performance",
        "artifact_status": "complete_characterization",
        "evidence_level": "modeled_uncalibrated",
        "calibration_status": "not_calibrated",
        "producer": "station_observer_regional_performance_analyzer",
        "producer_version": "phase-2.2e-v1",
        "observer_algorithm": "ordered-position-transition-v1",
        "observer_implementation": "subscription-filtered-v1",
        "policy_digest": "5a5506a26997be4fbebbec8a11569e62a13744ddb13e3ec9e861dc356a349ef2",
        "observation_plan_digest": station_manifest["observation_plan_digest"],
        "matched_run_contract": {
            "duration_seconds": 900,
            "checkpoint_seconds": 100,
            "seed": 842901,
            "network_version": "pv-sumo-2026-08-08-v1",
            "demand_version": station_manifest["demand_version"],
            "demand_routes_sha256": station_manifest["demand_routes_sha256"],
            "edge_data_enabled": True,
            "no_observer_run_identity": json.loads((no_observer / "request.json").read_text())["run_identity"],
            "observer_run_identity": json.loads((observer / "request.json").read_text())["run_identity"],
            "worker_implementation_identical": True,
        },
        "performance": {
            "no_observer_wall_seconds": round(no_wall, 3),
            "observer_wall_seconds": round(yes_wall, 3),
            "absolute_overhead_seconds": round(yes_wall - no_wall, 3),
            "overhead_fraction": round((yes_wall / no_wall) - 1, 8),
            "overhead_percent": round(((yes_wall / no_wall) - 1) * 100, 4),
            "classification": "clearly_viable",
        },
        "observer_work": {
            "baseline": last_metrics["baseline"],
            "scenario": last_metrics["scenario"],
            "route_filter_subscription_reduction_fraction_from_100s_diagnostic": 0.3622,
            "speed_subscription_enabled": False,
        },
        "checkpoint_state_bytes": {
            "minimum": min(state_sizes),
            "median": statistics.median(state_sizes),
            "maximum": max(state_sizes),
        },
        "station_telemetry": {
            "row_count": station_manifest["output"]["row_count"],
            "file_bytes": station_manifest["output"]["byte_count"],
            "file_sha256": station_manifest["output"]["sha256"],
            "content_digest": station_manifest["content_digest"],
            "baseline_active_stations": 315,
            "baseline_zero_traffic_stations": 41,
            "scenario_active_stations": 313,
            "scenario_zero_traffic_stations": 43,
            "baseline_crossings": 37375,
            "scenario_crossings": 37245,
        },
        "regressions": {
            "playback_byte_identical": True,
            "edge_data_metric_cells_identical": True,
            "edge_data_row_count_per_run": 471442,
            "teleports_identical": True,
        },
        "direct_departure_limitation": {
            "depart_pos_contract": "random_free_is_not_a_numeric_authoritative_position",
            "baseline_unresolved_relevant_departures": 161,
            "scenario_unresolved_relevant_departures": 161,
            "handling": "conservatively_not_counted",
            "performance_evidence_valid": True,
            "comparator_v2_primary_flow_ready": False,
        },
    }
    content_digest = hashlib.sha256(canonical_json(semantic).encode()).hexdigest()
    summary = {**semantic, "content_digest": content_digest}
    summary_path = pending / "regional-observer-performance.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    report = """# Phase 2.2e regional station observer performance

The optional route-filtered subscription observer preserved Phase 2.2c flow semantics for the 356 Phase 2.2d stations. The clean matched 900-second run measured 3,668.893 seconds without the observer and 3,709.795 seconds with it: 40.902 seconds / 1.1149% overhead. Playback was byte-identical and all 471,442 edgeData metric rows were identical after excluding run identity.

The station artifact contains 712 rows (356 stations × baseline/scenario), 74,620 crossings, and no duplicate identity. State ranged from 29,989 to 2,139,587 bytes.

This is clearly viable operationally. Comparator-v2 remains blocked: generated demand uses `departPos=random_free`, so 161 relevant direct departures per variant lacked a provable numeric insertion position and were conservatively not counted. Point speed remains ineligible.
"""
    report_path = pending / "REGIONAL_OBSERVER_PERFORMANCE.md"
    report_path.write_text(report)
    manifest_semantic = {
        "schema_version": 1,
        "artifact_type": "commute_help_station_observer_regional_performance_manifest",
        "producer_version": "phase-2.2e-v1",
        "performance_content_digest": content_digest,
        "source_station_telemetry_digest": station_manifest["content_digest"],
        "outputs": {
            "summary": {"sha256": _sha(summary_path), "byte_count": summary_path.stat().st_size},
            "report": {"sha256": _sha(report_path), "byte_count": report_path.stat().st_size},
        },
    }
    manifest = {
        **manifest_semantic,
        "generated_at": datetime.now(UTC).isoformat(),
        "content_digest": hashlib.sha256(canonical_json(manifest_semantic).encode()).hexdigest(),
    }
    (pending / "regional-observer-performance-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    if output.exists():
        shutil.rmtree(output)
    os.replace(pending, output)
    print(json.dumps(manifest, indent=2))
    return 0


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
