"""Compile reviewed PORTAL matches and register selectable local profiles."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from backend.app.services.portal_profile_compiler import (
    COMPILER_VERSION,
    PortalProfileCompileError,
    compile_profiles,
    register_profiles,
)


LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")


class RunLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.started)
        stopwatch = f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"
        line = f"[{datetime.now(LOCAL_TIMEZONE).isoformat()}] [+{stopwatch}] {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build compact PORTAL weekday AM/PM profiles and register them locally."
    )
    parser.add_argument(
        "--campaign",
        type=Path,
        default=Path("data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day"),
    )
    parser.add_argument(
        "--processed",
        type=Path,
        default=Path("data/traffic/processed/portland-vancouver-core-corridor-v2-full-day"),
    )
    parser.add_argument("--graph-dir", type=Path, default=Path("data/graphs"))
    parser.add_argument("--database", type=Path, default=Path("data/app.db"))
    parser.add_argument(
        "--matches",
        type=Path,
        help="Reviewed station-edge matches; defaults to the processed campaign directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Artifact directory; useful for candidate-graph builds that must not replace live profiles.",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Build and validate artifacts without changing SQLite.",
    )
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    processed = args.processed.resolve()
    graph_directory = args.graph_dir.resolve()
    output = (args.output or processed / "profiles").resolve()
    logger = RunLogger(output / "profile-build.log")
    logger.write(f"START {COMPILER_VERSION}")
    try:
        graph_manifest = json.loads(
            (graph_directory / "graph-manifest.json").read_text(encoding="utf-8")
        )
        campaign_manifest = json.loads(
            (campaign / "campaign-manifest.json").read_text(encoding="utf-8")
        )
        match_path = (
            args.matches.resolve()
            if args.matches
            else processed / "station-matching" / "station-edge-matches.parquet"
        )
        match_report_path = match_path.parent / "station-match-report.json"
        if not match_path.exists() or not match_report_path.exists():
            raise PortalProfileCompileError(
                "Run npm run traffic:match-stations before compiling profiles."
            )
        report = compile_profiles(
            observations_directory=processed / "observations",
            matches_path=match_path,
            edges_path=graph_directory / "edges.parquet",
            output_directory=output,
            graph_version=str(graph_manifest["graph_version"]),
            source_campaign=str(campaign_manifest["config"]["name"]),
            log=logger.write,
        )
        if not args.no_register:
            logger.write("Registering the two versioned profiles in local SQLite")
            register_profiles(
                report=report,
                database_path=args.database.resolve(),
                campaign_manifest_path=campaign / "campaign-manifest.json",
                match_report_path=match_report_path,
            )
        logger.write(
            f"DONE profiles={len(report['profiles'])} artifact_rows={report['artifact_rows']:,} "
            f"registered={'no' if args.no_register else 'yes'}"
        )
    except (OSError, KeyError, ValueError, PortalProfileCompileError) as error:
        logger.write(f"STOPPED safely: {error}")
        return 1
    print(f"Build report: {(output / 'profile-build-report.md').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
