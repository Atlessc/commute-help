"""Audit graph-derived state against a candidate graph without mutating live data."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping

from backend.app.schemas.closure_presets import ClosurePresetCatalog
from backend.app.schemas.scenarios import ScenarioContent


def _osm_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return tuple(sorted(str(item) for item in value if item is not None))


class EdgeRematcher:
    """Conservative directed-edge rematcher for graphs from one frozen source."""

    def __init__(self, edges: gpd.GeoDataFrame, sumo_map: pd.DataFrame) -> None:
        self.by_identity: dict[tuple[str, str, int], Any] = {}
        self.by_fingerprint: dict[tuple[str, tuple[str, ...]], list[Any]] = defaultdict(list)
        for _, edge in edges.iterrows():
            self.by_identity[(str(edge.u), str(edge.v), int(edge.key))] = edge
            key = (str(edge.geometry_fingerprint), _osm_ids(edge.osm_way_ids))
            self.by_fingerprint[key].append(edge)
        self.sumo_status = (
            sumo_map.groupby("app_edge_id", sort=False)["status"].first().to_dict()
        )

    def match(self, snapshot: dict[str, Any]) -> tuple[str, Any | None, str]:
        identity = (str(snapshot["u"]), str(snapshot["v"]), int(snapshot["key"]))
        candidate = self.by_identity.get(identity)
        expected_osm = _osm_ids(snapshot.get("osm_way_ids", []))
        expected_fingerprint = str(snapshot.get("geometry_fingerprint", ""))
        if candidate is not None:
            same_osm = _osm_ids(candidate.osm_way_ids) == expected_osm
            same_fingerprint = str(candidate.geometry_fingerprint) == expected_fingerprint
            if same_osm and same_fingerprint:
                return "accepted", candidate, "directed_identity_osm_geometry"
            return "review", candidate, "directed_identity_metadata_changed"
        alternatives = self.by_fingerprint.get((expected_fingerprint, expected_osm), [])
        if len(alternatives) == 1:
            return "accepted", alternatives[0], "unique_osm_geometry"
        if alternatives:
            return "review", None, "ambiguous_osm_geometry"
        return "failed", None, "no_high_confidence_match"

    def sumo_ready(self, edge: Any | None) -> bool:
        return edge is not None and self.sumo_status.get(str(edge.edge_id)) == "accepted"


def _edge_snapshot(edge: Any, existing: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    result.update(
        {
            "edge_id": str(edge.edge_id),
            "u": str(edge.u),
            "v": str(edge.v),
            "key": int(edge.key),
            "road_name": str(edge.road_name),
            "road_class": str(edge.road_class),
            "osm_way_ids": list(_osm_ids(edge.osm_way_ids)),
            "geometry_fingerprint": str(edge.geometry_fingerprint),
            "lanes": float(edge.lanes),
            "maxspeed_kph": float(edge.maxspeed_kph),
        }
    )
    return result


def _migrate_content(
    content: dict[str, Any],
    *,
    graph_version: str,
    rematcher: EdgeRematcher,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    migrated = deepcopy(content)
    outcomes: list[dict[str, Any]] = []
    migrated["graph_version"] = graph_version

    for endpoint_name in ("origin", "destination"):
        endpoint = migrated.get(endpoint_name)
        if not endpoint or not endpoint.get("edge"):
            continue
        status, edge, method = rematcher.match(endpoint["edge"])
        outcomes.append({"kind": endpoint_name, "status": status, "method": method})
        if status == "accepted" and edge is not None:
            endpoint["edge"] = _edge_snapshot(edge, endpoint["edge"])

    for section_index, section in enumerate(migrated.get("closures", [])):
        selection = section["selection"]
        selection["graph_version"] = graph_version
        selected_before = set(section["selected_edge_ids"])
        selected_after: list[str] = []
        for direction in selection["directions"]:
            old_id = str(direction["edge"]["edge_id"])
            status, edge, method = rematcher.match(direction["edge"])
            is_selected = old_id in selected_before
            sumo_ready = rematcher.sumo_ready(edge) if is_selected else None
            outcomes.append(
                {
                    "kind": "closure",
                    "section_index": section_index,
                    "selected": is_selected,
                    "status": status,
                    "method": method,
                    "sumo_ready": sumo_ready,
                }
            )
            if status == "accepted" and edge is not None:
                direction["edge"] = _edge_snapshot(edge, direction["edge"])
                direction["geometry"] = mapping(edge.geometry)
                if is_selected:
                    selected_after.append(str(edge.edge_id))
        if len(selected_after) == len(selected_before):
            section["selected_edge_ids"] = selected_after
            selection["selected_edge_id"] = selected_after[0]
    return migrated, outcomes


def _audit_derived_artifact(
    path: Path,
    rematcher: EdgeRematcher,
    *,
    require_metadata: bool,
) -> dict[str, Any]:
    frame = pd.read_parquet(path)
    counts: Counter[str] = Counter()
    for row in frame.itertuples(index=False):
        if pd.isna(row.u) or pd.isna(row.v) or pd.isna(row.key) or str(row.key).strip() == "":
            counts["source_unmatched"] += 1
            continue
        snapshot = {"u": row.u, "v": row.v, "key": row.key}
        if require_metadata:
            snapshot["osm_way_ids"] = row.osm_way_ids
            snapshot["geometry_fingerprint"] = row.geometry_fingerprint
        identity = (str(row.u), str(row.v), int(row.key))
        edge = rematcher.by_identity.get(identity)
        if edge is None:
            counts["failed"] += 1
        elif require_metadata and (
            _osm_ids(edge.osm_way_ids) != _osm_ids(snapshot["osm_way_ids"])
            or str(edge.geometry_fingerprint) != str(snapshot["geometry_fingerprint"])
        ):
            counts["review"] += 1
        else:
            counts["accepted"] += 1
    return {"path": str(path), "rows": len(frame), "status_counts": dict(counts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-edges", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--sumo-edge-map", type=Path, required=True)
    parser.add_argument("--preset-catalog", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--station-matches", type=Path, required=True)
    parser.add_argument("--edge-priors", type=Path, required=True)
    parser.add_argument("--edge-flow-seeds", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    log_path = args.output_dir / "migration.log"

    def log(message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat()}] [+{monotonic() - started:0.3f}s] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    manifest = json.loads(args.candidate_manifest.read_text(encoding="utf-8"))
    graph_version = str(manifest["graph_version"])
    log("LOAD candidate graph and SUMO edge map")
    edges = gpd.read_parquet(args.candidate_edges)
    sumo_map = pd.read_parquet(args.sumo_edge_map)
    rematcher = EdgeRematcher(edges, sumo_map)

    log("MIGRATE closure preset candidate")
    catalog = json.loads(args.preset_catalog.read_text(encoding="utf-8"))
    preset_outcomes: list[dict[str, Any]] = []
    for preset in catalog["presets"]:
        migrated, outcomes = _migrate_content(
            {"graph_version": preset["graph_version"], "closures": preset["sections"]},
            graph_version=graph_version,
            rematcher=rematcher,
        )
        preset["graph_version"] = graph_version
        preset["sections"] = migrated["closures"]
        preset_outcomes.extend({"preset_id": preset["id"], **item} for item in outcomes)
    ClosurePresetCatalog.model_validate(catalog)
    (args.output_dir / "closure-presets.candidate.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )

    log("AUDIT saved scenarios without modifying SQLite")
    scenario_outcomes: list[dict[str, Any]] = []
    scenario_dir = args.output_dir / "scenario-candidates"
    scenario_dir.mkdir(exist_ok=True)
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id, content_json FROM scenarios").fetchall()
    finally:
        connection.close()
    for scenario_id, content_json in rows:
        migrated, outcomes = _migrate_content(
            json.loads(content_json), graph_version=graph_version, rematcher=rematcher
        )
        valid = all(item["status"] == "accepted" for item in outcomes)
        if valid:
            ScenarioContent.model_validate(migrated)
            (scenario_dir / f"{scenario_id}.json").write_text(
                json.dumps(migrated, indent=2) + "\n", encoding="utf-8"
            )
        scenario_outcomes.extend(
            {"scenario_id": scenario_id, "candidate_written": valid, **item}
            for item in outcomes
        )

    log("REVALIDATE graph-derived traffic artifacts")
    derived = {
        "station_matches": _audit_derived_artifact(
            args.station_matches, rematcher, require_metadata=False
        ),
        "edge_priors": _audit_derived_artifact(
            args.edge_priors, rematcher, require_metadata=True
        ),
        "edge_flow_seeds": _audit_derived_artifact(
            args.edge_flow_seeds, rematcher, require_metadata=False
        ),
    }
    preset_counts = Counter(item["status"] for item in preset_outcomes)
    selected_closures = [
        item for item in preset_outcomes if item["kind"] == "closure" and item["selected"]
    ]
    scenario_counts = Counter(item["status"] for item in scenario_outcomes)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_graph_version": graph_version,
        "preset_status_counts": dict(preset_counts),
        "selected_preset_closures": {
            "count": len(selected_closures),
            "all_rematched": all(item["status"] == "accepted" for item in selected_closures),
            "all_sumo_ready": all(item["sumo_ready"] is True for item in selected_closures),
        },
        "scenario_count": len(rows),
        "scenario_status_counts": dict(scenario_counts),
        "scenario_candidates_written": len(list(scenario_dir.glob("*.json"))),
        "derived_artifacts": derived,
        "live_data_mutated": False,
        "promotion_ready": (
            all(item["status"] == "accepted" for item in preset_outcomes)
            and all(item["sumo_ready"] is True for item in selected_closures)
            and all(item["status"] == "accepted" for item in scenario_outcomes)
            and all(
                artifact["status_counts"].get("review", 0) == 0
                and artifact["status_counts"].get("failed", 0) == 0
                for artifact in derived.values()
            )
        ),
    }
    (args.output_dir / "migration-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "migration-report.md").write_text(
        "\n".join(
            [
                "# Candidate graph migration audit",
                "",
                f"- Candidate graph: `{graph_version}`",
                f"- Preset edge outcomes: {dict(preset_counts)}",
                f"- Selected preset closures all SUMO-ready: {report['selected_preset_closures']['all_sumo_ready']}",
                f"- Saved scenarios: {len(rows)}; edge outcomes: {dict(scenario_counts)}",
                f"- Scenario candidates written: {report['scenario_candidates_written']}",
                f"- Promotion ready: **{report['promotion_ready']}**",
                "- Live preset catalog and SQLite database were not modified.",
                "",
                "## Graph-derived artifacts",
                "",
                *[
                    f"- {name}: {item['rows']:,} rows; {item['status_counts']}"
                    for name, item in derived.items()
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(f"COMPLETE promotion_ready={report['promotion_ready']}")
    return 0 if report["promotion_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
