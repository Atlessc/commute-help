"""Build a review-only PORTAL station to directed graph edge crosswalk."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.services.portal_station_matcher import (
    MATCHER_VERSION,
    PortalStationMatchError,
    load_portal_stations,
    match_stations,
    observed_station_ids,
    prepare_edges,
    report_markdown,
    summarize_matches,
)


LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")


class RunLogger:
    def __init__(self, log_path: Path) -> None:
        self.started_at = datetime.now(LOCAL_TIMEZONE)
        self.started_monotonic = time.monotonic()
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        now = datetime.now(LOCAL_TIMEZONE)
        elapsed = int(time.monotonic() - self.started_monotonic)
        stopwatch = f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"
        line = f"[{now.isoformat()}] [+{stopwatch}] {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def run_matcher(
    *,
    campaign_directory: Path,
    processed_directory: Path,
    graph_directory: Path,
    output_directory: Path,
) -> dict[str, Any]:
    output_directory.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(output_directory / "station-matching.log")
    logger.write(f"START {MATCHER_VERSION}")
    manifest = _read_json(graph_directory / "graph-manifest.json")
    graph_version = str(manifest.get("graph_version") or "")
    if not graph_version:
        raise PortalStationMatchError("The graph manifest has no graph_version.")
    source_manifest = _read_json(campaign_directory / "campaign-manifest.json")
    source_name = str(source_manifest.get("config", {}).get("name") or "")
    if not source_name:
        raise PortalStationMatchError("The campaign manifest has no config.name.")

    logger.write("Scanning finalized Parquet station IDs at the intended station grain")
    station_ids = observed_station_ids(processed_directory / "observations")
    logger.write(f"Found {len(station_ids):,} distinct observed stations")
    stations = load_portal_stations(
        campaign_directory / "metadata" / "stations.json",
        campaign_directory / "metadata" / "highways.json",
        station_ids,
    )
    logger.write("Loading and projecting candidate graph edges")
    edges = prepare_edges(graph_directory / "edges.parquet")
    logger.write(f"Prepared {len(edges):,} plausible directed road edges")
    logger.write("Scoring direction, distance, route, road-form, and ambiguity evidence")
    matches = match_stations(stations, edges)
    report = summarize_matches(
        matches,
        graph_version=graph_version,
        source_campaign=source_name,
    )
    report["generated_at"] = datetime.now(LOCAL_TIMEZONE).isoformat()
    report["inputs"] = {
        "campaign_manifest_sha256": _sha256_file(
            campaign_directory / "campaign-manifest.json"
        ),
        "station_metadata_sha256": _sha256_file(
            campaign_directory / "metadata" / "stations.json"
        ),
        "highway_metadata_sha256": _sha256_file(
            campaign_directory / "metadata" / "highways.json"
        ),
        "graph_edges_sha256": _sha256_file(graph_directory / "edges.parquet"),
    }

    logger.write("Writing atomic CSV, Parquet, JSON, and Markdown review artifacts")
    _write_csv_atomic(output_directory / "station-edge-matches.csv", matches)
    _write_parquet_atomic(output_directory / "station-edge-matches.parquet", matches)
    _write_json_atomic(output_directory / "station-match-report.json", report)
    _write_text_atomic(
        output_directory / "station-match-report.md",
        report_markdown(report, matches),
    )
    logger.write(
        "DONE "
        f"accepted={report['accepted_count']:,} review={report['review_count']:,} "
        f"unmatched={report['unmatched_count']:,}; SQLite unchanged"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Match finalized PORTAL stations to directed graph edges and write a review-only report."
        )
    )
    parser.add_argument(
        "--campaign",
        type=Path,
        default=Path(
            "data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day"
        ),
    )
    parser.add_argument(
        "--processed",
        type=Path,
        default=Path(
            "data/traffic/processed/portland-vancouver-core-corridor-v2-full-day"
        ),
    )
    parser.add_argument("--graph-dir", type=Path, default=Path("data/graphs"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.processed / "station-matching"
    try:
        report = run_matcher(
            campaign_directory=args.campaign.resolve(),
            processed_directory=args.processed.resolve(),
            graph_directory=args.graph_dir.resolve(),
            output_directory=output.resolve(),
        )
    except PortalStationMatchError as error:
        print(f"PORTAL station matching stopped safely: {error}")
        return 1
    print(f"Review report: {(output / 'station-match-report.md').resolve()}")
    # Review findings are data-quality output, not an execution failure. Downstream
    # compilation still excludes every row that did not pass automatically.
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PortalStationMatchError(f"Could not read {path}.") from error
    if not isinstance(value, dict):
        raise PortalStationMatchError(f"Expected a JSON object in {path}.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True))


def _write_csv_atomic(path: Path, frame: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _write_parquet_atomic(path: Path, frame: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
