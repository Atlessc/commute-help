"""Materialize immutable projected geometry for comparison-v2 synthetic boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import sumolib
from pyproj import Transformer
from shapely.geometry import LineString

from backend.app.schemas.sumo_station_cross_sections_v2_r3 import (
    SyntheticBoundaryGeometryManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

EXPECTED_SYNTHETIC_IDS = {
    "1009562564-AddedOffRampEdge",
    "236943237#0-AddedOnRampEdge",
    "23552317#2-AddedOnRampEdge",
}
GEOMETRY_PARQUET = "synthetic-boundary-member-geometries.parquet"
GEOMETRY_SUMMARY = "summary.json"
GEOMETRY_MANIFEST = "manifest.json"
WGS84 = "EPSG:4326"
PROJECTED_CRS = "EPSG:32610"


class SyntheticBoundaryGeometryError(RuntimeError):
    """Synthetic-boundary geometry cannot satisfy frozen structural lineage."""


def build_synthetic_boundary_geometries_v1(
    *,
    comparison_directory: Path,
    comparison_validation_directory: Path,
    sumo_network_path: Path,
    sumo_network_manifest_path: Path,
    output_directory: Path,
) -> SyntheticBoundaryGeometryManifestV1:
    if output_directory.exists():
        raise SyntheticBoundaryGeometryError(f"immutable output exists: {output_directory}")
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        raise SyntheticBoundaryGeometryError(f"preserved pending output exists: {pending}")

    comparison_manifest_path = comparison_directory / "comparison-relation-manifest.json"
    comparison_parquet_path = comparison_directory / "app-sumo-comparison-relations.parquet"
    comparison_summary_path = comparison_validation_directory / "summary.json"
    comparison_provenance_path = comparison_validation_directory / "provenance.json"
    network_manifest = json.loads(sumo_network_manifest_path.read_text(encoding="utf-8"))
    comparison_manifest = json.loads(comparison_manifest_path.read_text(encoding="utf-8"))
    comparison_summary = json.loads(comparison_summary_path.read_text(encoding="utf-8"))
    network_sha = sha256_file(sumo_network_path)
    if network_manifest["artifact"]["sha256"] != network_sha:
        raise SyntheticBoundaryGeometryError("SUMO network differs from frozen manifest")
    if comparison_manifest["sumo_network_sha256"] != network_sha:
        raise SyntheticBoundaryGeometryError("comparison-v2 SUMO lineage differs")
    if comparison_summary.get("status") != "PASS_TRAFFIC_BLIND_STATIC_VALIDATION":
        raise SyntheticBoundaryGeometryError("comparison-v2 validation did not pass")
    guards = comparison_summary.get("scientific_guards", {})
    if any(
        bool(guards.get(name))
        for name in (
            "traffic_values_loaded",
            "sumo_behavior_executed",
            "development_loaded",
            "blind_loaded",
            "methodology_modified",
            "builder_relaxed",
            "clustering_case_repaired",
        )
    ):
        raise SyntheticBoundaryGeometryError("comparison-v2 scientific guard failed")

    changed = comparison_summary.get("changed_relations", [])
    ownership_by_member: dict[str, dict[str, Any]] = {}
    for relation in changed:
        for evidence in relation.get("ownership_evidence", []):
            edge_id = str(evidence["synthetic_sumo_edge_id"])
            if edge_id in ownership_by_member:
                raise SyntheticBoundaryGeometryError(f"duplicate synthetic owner: {edge_id}")
            ownership_by_member[edge_id] = {
                **evidence,
                "relation_id": str(relation["relation_id"]),
            }
    if set(ownership_by_member) != EXPECTED_SYNTHETIC_IDS:
        raise SyntheticBoundaryGeometryError(
            f"unexpected synthetic member set: {sorted(ownership_by_member)}"
        )

    relations = pq.read_table(comparison_parquet_path).to_pandas()
    relations["app_edge_id"] = relations["app_edge_id"].astype(str)
    relation_by_app = relations.set_index("app_edge_id", drop=False)
    network = sumolib.net.readNet(str(sumo_network_path), withInternal=False)
    transformer = Transformer.from_crs(WGS84, PROJECTED_CRS, always_xy=True)
    rows: list[dict[str, Any]] = []
    for edge_id in sorted(ownership_by_member):
        owner = ownership_by_member[edge_id]
        app_edge_id = str(owner["app_edge_id"])
        relation = relation_by_app.loc[app_edge_id]
        ordered = parse_json_strings(relation["ordered_sumo_edge_ids_json"])
        if edge_id not in ordered:
            raise SyntheticBoundaryGeometryError(f"synthetic member absent from owner chain: {edge_id}")
        evidence_by_id = {
            str(value["sumo_edge_id"]): value
            for value in json.loads(str(relation["member_topology_evidence_json"]))
        }
        edge = network.getEdge(edge_id)
        if edge.getFunction() == "internal":
            raise SyntheticBoundaryGeometryError(f"synthetic member is internal: {edge_id}")
        if edge.getType() != "highway.motorway":
            raise SyntheticBoundaryGeometryError(f"synthetic member type differs: {edge_id}")
        shape = edge.getShape()
        if len(shape) < 2:
            raise SyntheticBoundaryGeometryError(f"synthetic member shape unusable: {edge_id}")
        geographic = [network.convertXY2LonLat(x, y) for x, y in shape]
        geometry = LineString(
            [transformer.transform(lon, lat) for lon, lat in geographic]
        )
        if geometry.is_empty or not math.isfinite(float(geometry.length)) or geometry.length <= 0:
            raise SyntheticBoundaryGeometryError(f"projected geometry unusable: {edge_id}")
        geometry_wkb = bytes(geometry.wkb)
        orig_ids = sorted(
            {
                value
                for lane in edge.getLanes()
                for value in lane.getParams().get("origId", "").split()
                if value
            }
        )
        if orig_ids != sorted(str(value) for value in owner["synthetic_orig_ids"]):
            raise SyntheticBoundaryGeometryError(f"origId provenance differs: {edge_id}")
        if edge.getFromNode().getID() != str(owner["synthetic_from_junction"]):
            raise SyntheticBoundaryGeometryError(f"from-junction provenance differs: {edge_id}")
        if edge.getToNode().getID() != str(owner["synthetic_to_junction"]):
            raise SyntheticBoundaryGeometryError(f"to-junction provenance differs: {edge_id}")
        static_length = evidence_by_id[edge_id].get("length_m")
        if static_length is None or not math.isfinite(float(static_length)) or float(static_length) <= 0:
            raise SyntheticBoundaryGeometryError(f"static length unusable: {edge_id}")
        rows.append(
            {
                "schema_version": 1,
                "sumo_edge_id": edge_id,
                "app_edge_id": app_edge_id,
                "relation_id": str(owner["relation_id"]),
                "boundary_role": str(owner["boundary"]),
                "synthetic_boundary_kind": str(owner["synthetic_boundary_kind"]),
                "ownership_rule": str(owner["ownership_rule"]),
                "orig_ids_json": canonical_json(orig_ids),
                "from_junction": edge.getFromNode().getID(),
                "to_junction": edge.getToNode().getID(),
                "edge_type": edge.getType(),
                "edge_function": edge.getFunction() or "external",
                "lane_ids_json": canonical_json(sorted(lane.getID() for lane in edge.getLanes())),
                "static_member_length_m": float(static_length),
                "projected_geometry_length_m": float(geometry.length),
                "projected_geometry_wkb": geometry_wkb,
                "projected_geometry_wkb_sha256": hashlib.sha256(geometry_wkb).hexdigest(),
                "source_sumo_network_sha256": network_sha,
                "geometry_construction": "frozen_sumo_xy_to_sumolib_wgs84_to_pyproj_epsg32610_to_shapely_wkb",
            }
        )
    frame = pd.DataFrame(rows).sort_values("sumo_edge_id", kind="mergesort").reset_index(drop=True)
    summary = {
        "status": "MATERIALIZED_STATIC_SYNTHETIC_BOUNDARY_GEOMETRY",
        "synthetic_member_count": len(frame),
        "synthetic_member_ids": list(frame["sumo_edge_id"]),
        "geometry_wkb_sha256_by_member": {
            str(row.sumo_edge_id): str(row.projected_geometry_wkb_sha256)
            for row in frame.itertuples(index=False)
        },
        "sumo_network_sha256": network_sha,
        "traffic_values_loaded": False,
        "sumo_behavior_executed": False,
        "development_loaded": False,
        "blind_loaded": False,
    }

    pending.mkdir(parents=True)
    parquet_path = pending / GEOMETRY_PARQUET
    summary_path = pending / GEOMETRY_SUMMARY
    manifest_path = pending / GEOMETRY_MANIFEST
    table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
        {
            b"schema_version": b"1",
            b"artifact": b"synthetic-boundary-member-projected-geometry",
            b"projected_crs": PROJECTED_CRS.encode(),
        }
    )
    pq.write_table(table, parquet_path, compression="zstd", compression_level=9)
    summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "commute_help_synthetic_boundary_member_projected_geometry",
        "artifact_status": "complete_static_geometry",
        "generated_at": datetime.now(UTC).isoformat(),
        "producer_name": "synthetic_boundary_member_geometry_materializer",
        "producer_version": "station-cross-sections-v2-r3-prerequisite-v1",
        "source_crs": WGS84,
        "projected_crs": PROJECTED_CRS,
        "geometry_construction": "frozen_sumo_xy_to_sumolib_wgs84_to_pyproj_epsg32610_to_shapely_wkb",
        "sumo_network_version": str(network_manifest["network_version"]),
        "sumo_network_sha256": network_sha,
        "sumo_network_manifest_sha256": sha256_file(sumo_network_manifest_path),
        "comparison_manifest_sha256": sha256_file(comparison_manifest_path),
        "comparison_parquet_sha256": sha256_file(comparison_parquet_path),
        "comparison_validation_summary_sha256": sha256_file(comparison_summary_path),
        "comparison_validation_provenance_sha256": sha256_file(comparison_provenance_path),
        "synthetic_member_ids": sorted(ownership_by_member),
        "geometry_output": file_identity(parquet_path, len(frame)),
        "summary_output": file_identity(summary_path),
        "row_content_sha256": frame_digest(frame),
    }
    payload["content_digest"] = content_digest(payload)
    manifest = SyntheticBoundaryGeometryManifestV1.model_validate(payload)
    manifest_path.write_text(
        canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    os.replace(pending, output_directory)
    return manifest


def parse_json_strings(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise SyntheticBoundaryGeometryError("expected JSON list")
    return [str(item) for item in parsed]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path, rows: int | None = None) -> dict[str, Any]:
    return {
        "relative_path": path.name,
        "sha256": sha256_file(path),
        "byte_count": path.stat().st_size,
        "row_count": rows,
    }


def frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        normalized = {
            key: value.hex() if isinstance(value, bytes) else value.item() if hasattr(value, "item") else value
            for key, value in row.items()
        }
        digest.update(canonical_json(normalized).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
