"""Run the Phase 1 topology, direction, and selected-closure SUMO gate."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import pandas as pd

from backend.app.services.sumo.network_service import SumoNetworkService


REPRESENTATIVE_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
}


def _warning_categories(path: Path) -> dict[str, int]:
    categories: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Warning:"):
            continue
        if "maxspeed:type" in line:
            categories["non_numeric_maxspeed_type"] += 1
        elif "width" in line and "could not be parsed" in line:
            categories["unparsed_width"] += 1
        elif "lane use specifier" in line:
            categories["unknown_lane_use"] += 1
        elif "roundabout" in line:
            categories["roundabout_rewrite"] += 1
        elif "junction" in line and "distance" in line:
            categories["junction_shape_offset"] += 1
        elif "turning radius" in line:
            categories["turn_radius_speed_reduction"] += 1
        else:
            categories["other"] += 1
    return dict(categories)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--edge-map", type=Path, required=True)
    parser.add_argument("--closure-catalog", type=Path, required=True)
    parser.add_argument("--netconvert-warnings", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    log_path = args.output_dir / "phase1-validation.log"

    def log(message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    log("LOAD checksum-verified SUMO network")
    service = SumoNetworkService(args.network, args.manifest, args.edge_map)
    service.load()
    assert service.network is not None
    mapping = pd.read_parquet(args.edge_map)

    catalog = json.loads(args.closure_catalog.read_text(encoding="utf-8"))
    selected_ids = [
        edge_id
        for preset in catalog["presets"]
        for section in preset["sections"]
        for edge_id in section["selected_edge_ids"]
    ]
    translated = service.translate_closure_edges(selected_ids)
    selected_sumo_ids = sorted({item for values in translated.values() for item in values})
    selected_topology: list[dict[str, Any]] = []
    for edge_id in selected_sumo_ids:
        edge = service.network.getEdge(edge_id)
        selected_topology.append(
            {
                "sumo_edge_id": edge_id,
                "lane_count": len(edge.getLanes()),
                "incoming_count": len(edge.getIncoming()),
                "outgoing_count": len(edge.getOutgoing()),
            }
        )

    by_app = mapping.groupby("app_edge_id", sort=False).first()
    class_report: dict[str, Any] = {}
    for road_class in sorted(REPRESENTATIVE_CLASSES):
        rows = by_app[by_app["road_class"] == road_class]
        accepted = rows[rows["status"] == "accepted"]
        class_report[road_class] = {
            "total": len(rows),
            "accepted": len(accepted),
            "accepted_percent": round(100 * len(accepted) / len(rows), 3) if len(rows) else 0,
            "representative_edge_id": (
                str(accepted.sort_values("match_score", ascending=False).index[0])
                if len(accepted)
                else None
            ),
        }

    warnings = _warning_categories(args.netconvert_warnings)
    directional_failures = int(
        (
            (mapping["status"] == "accepted")
            & (mapping["direction_error_degrees"].fillna(181) > 45)
        ).sum()
    )
    topology_failures = [
        item
        for item in selected_topology
        if item["lane_count"] < 1
        or item["incoming_count"] < 1
        or item["outgoing_count"] < 1
    ]
    gate_passed = (
        directional_failures == 0
        and not topology_failures
        and all(item["representative_edge_id"] for item in class_report.values())
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "network_version": service.manifest["network_version"],
        "gate_passed": gate_passed,
        "selected_app_closure_edges": len(set(selected_ids)),
        "selected_sumo_edges": len(selected_sumo_ids),
        "selected_topology_failures": topology_failures,
        "accepted_directional_failures": directional_failures,
        "representative_road_classes": class_report,
        "netconvert_warning_categories": warnings,
        "warning_policy": "reviewed_not_silenced",
    }
    (args.output_dir / "phase1-validation-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "phase1-validation-report.md").write_text(
        "\n".join(
            [
                "# SUMO Phase 1 network gate",
                "",
                f"- Gate: **{'PASS' if gate_passed else 'FAIL'}**",
                f"- Selected closure edges: {len(set(selected_ids))} app / {len(selected_sumo_ids)} SUMO",
                f"- Selected topology failures: {len(topology_failures)}",
                f"- Accepted direction failures: {directional_failures}",
                f"- netconvert warnings reviewed by category: {warnings}",
                "",
                "Generated signal plans remain uncalibrated and are not promoted as historical truth.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(f"COMPLETE gate_passed={gate_passed}")
    return 0 if gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
