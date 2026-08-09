"""Validate and normalize one immutable regional OD agency delivery."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from backend.app.services.regional_od_intake_service import (
    RegionalOdIntakeError,
    report_markdown,
    validate_regional_od_intake,
)


ROOT = Path(__file__).resolve().parents[1]


class IntakeLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str, level: str = "INFO") -> None:
        elapsed = time.monotonic() - self.started
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        line = (
            f"[{timestamp}] [+{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}] "
            f"{level} {message}"
        )
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify regional OD provenance, units, geography, and release terms."
    )
    parser.add_argument(
        "--campaign",
        type=Path,
        required=True,
        help="Immutable regional-OD campaign directory containing campaign-manifest.json.",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Write reports but do not write normalized Parquet artifacts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    campaign = args.campaign.resolve()
    manifest_path = (args.manifest or campaign / "campaign-manifest.json").resolve()
    identity = _manifest_identity(manifest_path)
    output = (
        args.output.resolve()
        if args.output is not None
        else ROOT / "data/traffic/processed/regional-od" / identity
    )
    output.mkdir(parents=True, exist_ok=True)
    logger = IntakeLogger(output / "intake.log")
    logger(f"START regional-OD intake campaign={identity}")
    try:
        od, zones, report = validate_regional_od_intake(campaign, manifest_path)
        _atomic_text(output / "intake-report.json", json.dumps(report, indent=2) + "\n")
        _atomic_text(output / "intake-report.md", report_markdown(report))
        logger(
            f"validated source rows={report['quality']['source_od_rows']:,} "
            f"zones={report['quality']['source_zone_rows']:,} status={report['status']}"
        )
        if not report["normalization_ready"]:
            logger(
                f"BLOCKED issues={len(report['blocking_issues'])}; normalized files withheld",
                "ERROR",
            )
            return 2
        if args.validate_only:
            logger("COMPLETE validate-only; normalized files intentionally not written")
            return 0
        assert od is not None and zones is not None
        _atomic_parquet(output / "od-demand.parquet", od)
        _atomic_parquet(output / "zones.parquet", zones)
        logger(
            f"COMPLETE wrote normalized OD rows={len(od):,} zones={len(zones):,}; "
            f"assignment_ready={report['assignment_ready']}"
        )
        return 0
    except RegionalOdIntakeError as error:
        logger(f"FAILED {error}", "ERROR")
        return 1


def _manifest_identity(path: Path) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        value = str(manifest["campaign_id"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SystemExit(f"Cannot determine campaign identity from {path}: {error}") from error
    safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in value)
    if not safe:
        raise SystemExit("campaign_id does not contain a usable output name")
    return safe


def _atomic_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _atomic_parquet(path: Path, frame: object) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
