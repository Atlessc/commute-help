"""Compile the completed PORTAL campaign into a versioned 24/7 schedule."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from backend.app.services.traffic_schedule_compiler import compile_traffic_schedule


NUMERIC_GATES = [
    "volume_mean",
    "volume_median",
    "volume_std",
    "volume_p10",
    "volume_p50",
    "volume_p85",
    "volume_p90",
    "volume_p95",
    "speed_mean",
    "speed_median",
    "speed_std",
    "speed_p10",
    "speed_p50",
    "speed_p85",
    "speed_p90",
    "speed_p95",
    "occupancy_mean",
    "occupancy_median",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path(
            "data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/observations"
        ),
    )
    parser.add_argument(
        "--station-matches",
        type=Path,
        default=Path(
            "data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-edge-matches.parquet"
        ),
    )
    parser.add_argument(
        "--graph-manifest", type=Path, default=Path("data/graphs/graph-manifest.json")
    )
    parser.add_argument(
        "--campaign-manifest",
        type=Path,
        default=Path(
            "data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json"
        ),
    )
    parser.add_argument(
        "--schedule-version", default="pv-portal-24x7-2026-08-08-v4"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("data/traffic/processed/schedules") / args.schedule_version
    if output.exists():
        raise ValueError("Schedule output already exists; use a new version")
    output.mkdir(parents=True)
    started = monotonic()
    log_path = output / "build.log"

    def log(message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    graph_manifest = json.loads(args.graph_manifest.read_text(encoding="utf-8"))
    campaign_manifest = json.loads(args.campaign_manifest.read_text(encoding="utf-8"))
    log(
        f"START schedule={args.schedule_version} graph={graph_manifest['graph_version']} "
        "mode=offline"
    )
    schedule, report = compile_traffic_schedule(
        observations_directory=args.observations,
        station_matches_path=args.station_matches,
        schedule_version=args.schedule_version,
        log=log,
    )
    log("VALIDATE coverage, physical bounds, quantile order, and evidence labels")
    expected_rows = report["accepted_station_count"] * 96 * 4
    null_counts = {column: int(schedule[column].isna().sum()) for column in NUMERIC_GATES}
    volume_ordered = bool(
        (
            schedule[["volume_p10", "volume_p50", "volume_p85", "volume_p90", "volume_p95"]]
            .diff(axis=1)
            .iloc[:, 1:]
            >= 0
        ).all().all()
    )
    speed_ordered = bool(
        (
            schedule[["speed_p10", "speed_p50", "speed_p85", "speed_p90", "speed_p95"]]
            .diff(axis=1)
            .iloc[:, 1:]
            >= 0
        ).all().all()
    )
    weekend = schedule[schedule["day_type"].isin(["saturday", "sunday"])]
    gate_passed = (
        len(schedule) == expected_rows
        and report["buckets_per_day_type"] == 96
        and set(report["day_types"]) == {"mon_thu", "friday", "saturday", "sunday"}
        and not any(null_counts.values())
        and bool((schedule["volume_mean"] >= 0).all())
        and bool(schedule["speed_mean"].between(0.1, 160).all())
        and volume_ordered
        and speed_ordered
        and set(weekend["evidence_level"].unique()) == {"modeled_unobserved"}
    )
    report.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "graph_version": graph_manifest["graph_version"],
            "osm_source_sha256": graph_manifest.get("osm_source_sha256"),
            "expected_schedule_rows": expected_rows,
            "numeric_null_counts": null_counts,
            "volume_quantiles_ordered": volume_ordered,
            "speed_quantiles_ordered": speed_ordered,
            "gate_passed": gate_passed,
        }
    )
    schedule_path = output / "schedule.parquet"
    partial = output / "schedule.parquet.part"
    schedule.to_parquet(partial, index=False, compression="zstd")
    os.replace(partial, schedule_path)
    manifest = {
        "schema_version": 1,
        "schedule_version": args.schedule_version,
        "created_at": datetime.now(UTC).isoformat(),
        "timezone": "America/Los_Angeles",
        "resolution_minutes": 15,
        "graph_version": graph_manifest["graph_version"],
        "osm_source_sha256": graph_manifest.get("osm_source_sha256"),
        "source_campaign": campaign_manifest.get("config", {}).get("name"),
        "source_campaign_manifest_sha256": _sha256(args.campaign_manifest),
        "station_matches_sha256": _sha256(args.station_matches),
        "network_access_used": False,
        "weekday_evidence": "observed_historical_input",
        "weekend_evidence": "modeled_unobserved",
        "artifact": {
            "filename": schedule_path.name,
            "sha256": _sha256(schedule_path),
            "size_bytes": schedule_path.stat().st_size,
            "rows": len(schedule),
        },
    }
    (output / "schedule-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (output / "validation-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (output / "validation-report.md").write_text(
        "\n".join(
            [
                f"# Traffic schedule validation — {args.schedule_version}",
                "",
                f"- Gate: **{'PASS' if gate_passed else 'FAIL'}**",
                f"- Source rows scanned: {report['scanned_rows']:,}",
                f"- Eligible matched rows: {report['accepted_observation_rows']:,}",
                f"- Accepted stations: {report['accepted_station_count']:,}",
                f"- App edges represented: {report['unique_app_edge_count']:,}",
                f"- Schedule rows: {len(schedule):,}",
                "- Weekday evidence: observed historical input (not yet a calibrated model result)",
                "- Weekend evidence: modeled unobserved fallback",
                "- Quantiles: deterministic bounded histograms",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(f"COMPLETE gate_passed={gate_passed} rows={len(schedule):,}")
    return 0 if gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
