"""Activate one locally validated schedule without copying its artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schedule_directory", type=Path)
    args = parser.parse_args()
    candidate = args.schedule_directory.resolve()
    report = json.loads((candidate / "validation-report.json").read_text(encoding="utf-8"))
    manifest = json.loads((candidate / "schedule-manifest.json").read_text(encoding="utf-8"))
    graph = json.loads(Path("data/graphs/graph-manifest.json").read_text(encoding="utf-8"))
    if report.get("gate_passed") is not True:
        raise ValueError("Refusing to activate a schedule that did not pass validation")
    if manifest.get("graph_version") != graph.get("graph_version"):
        raise ValueError("Schedule and active graph versions differ")
    active = Path("data/traffic/processed/schedules/active")
    if active.exists() or active.is_symlink():
        raise ValueError("An active traffic schedule already exists")
    if candidate.parent != active.parent.resolve():
        raise ValueError("Schedule must be a sibling of the active link")
    temporary = active.with_name("active.schedule-part")
    temporary.symlink_to(candidate.name, target_is_directory=True)
    os.replace(temporary, active)
    print(f"Activated traffic schedule {manifest['schedule_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
