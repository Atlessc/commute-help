"""Promote a passed Phase 1 candidate with recoverable local backups."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from backend.app.services.portal_profile_compiler import register_profiles


def _passed(path: Path, key: str) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get(key) is not True:
        raise ValueError(f"Refusing promotion because {path} does not have {key}=true")
    return report


def _replace_directory(source: Path, target: Path, backup: Path) -> None:
    temporary = target.with_name(target.name + ".phase1-part")
    if temporary.exists():
        raise ValueError(f"Temporary promotion path already exists: {temporary}")
    shutil.copytree(source, temporary)
    if target.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, backup)
    os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-graph", type=Path, required=True)
    parser.add_argument("--candidate-preset", type=Path, required=True)
    parser.add_argument("--migration-dir", type=Path, required=True)
    parser.add_argument("--sumo-network-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    started = monotonic()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = Path("data/backups") / f"sumo-phase1-promotion-{stamp}"
    promotion_dir = Path("data/sumo/promotions") / stamp
    promotion_dir.mkdir(parents=True, exist_ok=False)
    log_path = promotion_dir / "promotion.log"

    def log(message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    migration_report = _passed(args.migration_dir / "migration-report.json", "promotion_ready")
    network_report = _passed(
        args.sumo_network_dir / "phase1-validation-report.json", "gate_passed"
    )
    graph_report = _passed(args.candidate_graph / "validation-report.json", "gate_passed")
    candidate_manifest = json.loads(
        (args.candidate_graph / "graph-manifest.json").read_text(encoding="utf-8")
    )
    if migration_report["candidate_graph_version"] != candidate_manifest["graph_version"]:
        raise ValueError("Migration audit and candidate graph versions differ")
    if not args.apply:
        log("PREFLIGHT PASS; rerun with --apply to promote")
        return 0

    graph_version = candidate_manifest["graph_version"]
    edge_prior_target = Path("data/traffic/processed/edge-priors") / graph_version
    background_target = Path("data/traffic/processed/background-seeds") / graph_version
    active = Path("data/sumo/networks/active")
    if edge_prior_target.exists() or background_target.exists():
        raise ValueError("Candidate-version traffic target already exists")
    if active.exists() or active.is_symlink():
        raise ValueError("Active SUMO network path already exists")
    if args.sumo_network_dir.resolve().parent != active.parent.resolve():
        raise ValueError("Active SUMO network must be a sibling of the active link")

    backup.mkdir(parents=True, exist_ok=False)
    log(f"BACKUP mutable application state to {backup}")
    shutil.copy2("data/app.db", backup / "app.db")
    shutil.copy2("data/presets/closure-presets.json", backup / "closure-presets.json")

    log("PROMOTE candidate app graph; old graph remains in backup")
    _replace_directory(args.candidate_graph, Path("data/graphs"), backup / "graphs")

    log("PROMOTE reviewed closure preset catalog atomically")
    preset_target = Path("data/presets/closure-presets.json")
    preset_part = preset_target.with_suffix(".json.phase1-part")
    shutil.copy2(args.candidate_preset, preset_part)
    os.replace(preset_part, preset_target)

    traffic_root = Path("data/traffic/processed/portland-vancouver-core-corridor-v2-full-day")
    candidate_traffic = args.migration_dir / "traffic"
    log("PROMOTE rebuilt station matches and PORTAL profiles")
    _replace_directory(
        candidate_traffic / "station-matching",
        traffic_root / "station-matching",
        backup / "traffic/station-matching",
    )
    _replace_directory(
        candidate_traffic / "profiles",
        traffic_root / "profiles",
        backup / "traffic/profiles",
    )

    shutil.copytree(candidate_traffic / "edge-priors", edge_prior_target)
    shutil.copytree(candidate_traffic / "background-seeds", background_target)

    log("REGISTER rebuilt selectable profiles in backed-up SQLite")
    profile_report = json.loads(
        (traffic_root / "profiles/profile-build-report.json").read_text(encoding="utf-8")
    )
    register_profiles(
        report=profile_report,
        database_path=Path("data/app.db"),
        campaign_manifest_path=Path(
            "data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json"
        ),
        match_report_path=traffic_root / "station-matching/station-match-report.json",
    )

    log("ACTIVATE checksum-verified SUMO network by local relative symlink")
    temporary_link = active.with_name("active.phase1-part")
    temporary_link.symlink_to(args.sumo_network_dir.name, target_is_directory=True)
    os.replace(temporary_link, active)

    manifest = {
        "schema_version": 1,
        "promoted_at": datetime.now(UTC).isoformat(),
        "graph_version": graph_version,
        "sumo_network_version": network_report["network_version"],
        "osm_source_sha256": candidate_manifest["osm_source_sha256"],
        "backup_path": str(backup),
        "saved_scenarios_mutated": False,
        "saved_scenario_policy": "rematch_on_load_then_save_new_revision",
        "model_evidence": "uncalibrated",
        "graph_validation": graph_report,
    }
    (promotion_dir / "promotion-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    log("COMPLETE Phase 1 candidate is active; no calibrated model exists yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
