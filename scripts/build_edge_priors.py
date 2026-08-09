"""Build the local all-edge evidence and prior artifact."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from backend.app.services.edge_prior_service import (
    EdgePriorBuildError,
    build_edge_priors,
    report_markdown,
)


ROOT = Path(__file__).resolve().parents[1]


class CampaignLogger:
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


def _latest_complete_campaign(root: Path) -> Path:
    candidates = sorted(root.glob("road-controls-*/campaign-manifest.json"), reverse=True)
    for manifest_path in candidates:
        try:
            if json.loads(manifest_path.read_text()).get("status") == "complete":
                return manifest_path.parent
        except (OSError, json.JSONDecodeError):
            continue
    raise EdgePriorBuildError(f"No complete authority-controls campaign found under {root}.")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build evidence-ranked priors for every directed graph edge."
    )
    parser.add_argument("--edges", type=Path, default=ROOT / "data/graphs/edges.parquet")
    parser.add_argument("--nodes", type=Path, default=ROOT / "data/graphs/nodes.parquet")
    parser.add_argument(
        "--graph-manifest", type=Path, default=ROOT / "data/graphs/graph-manifest.json"
    )
    parser.add_argument("--campaign", type=Path)
    parser.add_argument(
        "--traffic-profiles",
        type=Path,
        default=ROOT
        / "data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/profiles/edge-bucket-profiles.parquet",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    graph_manifest = json.loads(args.graph_manifest.read_text())
    graph_version = str(graph_manifest["graph_version"])
    campaign = args.campaign or _latest_complete_campaign(
        ROOT / "data/traffic/raw/road-controls"
    )
    output = args.output or (
        ROOT / "data/traffic/processed/edge-priors" / graph_version
    )
    output.mkdir(parents=True, exist_ok=True)
    logger = CampaignLogger(output / "build.log")
    logger(
        f"START edge-prior build graph={graph_version} campaign={campaign.name}"
    )
    try:
        priors, review, report = build_edge_priors(
            edges_path=args.edges,
            nodes_path=args.nodes,
            graph_version=graph_version,
            campaign_directory=campaign,
            traffic_profiles_path=args.traffic_profiles,
            progress=logger,
        )
        parquet_path = output / "edge-priors.parquet"
        temporary_parquet = output / "edge-priors.parquet.part"
        priors.to_parquet(temporary_parquet, index=False)
        temporary_parquet.replace(parquet_path)
        review_path = output / "authority-match-review.csv"
        temporary_review = output / "authority-match-review.csv.part"
        review.to_csv(temporary_review, index=False)
        temporary_review.replace(review_path)
        _atomic_text(output / "quality-report.json", json.dumps(report, indent=2) + "\n")
        _atomic_text(output / "quality-report.md", report_markdown(report))
        logger(
            f"COMPLETE wrote {len(priors):,} edge priors and {len(review):,} review rows"
        )
        return 0
    except Exception as error:
        logger(f"FAILED {error}", "ERROR")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
