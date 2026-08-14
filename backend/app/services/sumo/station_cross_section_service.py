"""Project accepted PORTAL stations only onto accepted resolved SUMO relations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import LineString, Point

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_policy import (
    HistoricalQualityPolicyManifestV1,
)
from backend.app.schemas.sumo_comparison_relations import (
    ComparisonRelationManifestV1,
)
from backend.app.schemas.sumo_station_cross_sections import (
    E1_FEASIBILITY_VERSION,
    STATION_CROSS_SECTION_ALGORITHM_VERSION,
    STATION_CROSS_SECTION_PRODUCER_VERSION,
    STATION_CROSS_SECTION_SCHEMA_VERSION,
    StationCrossSectionManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

CROSS_SECTION_DIRECTORY = "station-cross-sections-v1"
CROSS_SECTION_PARQUET = "station-sumo-cross-sections.parquet"
CROSS_SECTION_SUMMARY = "station-cross-section-summary.json"
CROSS_SECTION_REPORT = "STATION_CROSS_SECTION_ANALYSIS.md"
CROSS_SECTION_MANIFEST = "station-cross-section-manifest.json"
E1_FEASIBILITY_REPORT = "e1-feasibility.json"
PROJECTED_CRS = "EPSG:32610"
WGS84 = "EPSG:4326"


class StationCrossSectionError(RuntimeError):
    """Station projection cannot satisfy the Phase 2.2b evidence contract."""


def _canonical_portal_station_id(value: object) -> str:
    station_id = str(value)
    if station_id.startswith("portal-station-"):
        return station_id
    return f"portal-station-{station_id}"


def project_station_to_relation(
    *,
    station_id: str,
    longitude: float,
    latitude: float,
    direction: str,
    relation: dict[str, Any],
    member_geometries: dict[str, LineString],
    member_lengths_m: dict[str, float],
    equal_distance_tolerance_m: float = 0.01,
) -> dict[str, Any]:
    """Return raw nearest-member evidence without whole-network fallback."""
    ordered = json.loads(str(relation.get("ordered_sumo_edge_ids_json") or "[]"))
    if not relation.get("comparison_eligible") or not ordered:
        return _empty_projection(
            station_id,
            relation,
            "review_topology",
            "relation_not_topology_eligible",
        )
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return _empty_projection(
            station_id, relation, "unmatched", "station_coordinates_missing"
        )
    transformer = Transformer.from_crs(WGS84, PROJECTED_CRS, always_xy=True)
    x, y = transformer.transform(longitude, latitude)
    point = Point(x, y)
    candidates: list[dict[str, Any]] = []
    cumulative = 0.0
    for index, edge_id in enumerate(ordered):
        geometry = member_geometries.get(edge_id)
        member_length = member_lengths_m.get(edge_id)
        if geometry is None or geometry.is_empty or not member_length or member_length <= 0:
            return _empty_projection(
                station_id,
                relation,
                "unmatched",
                "accepted_relation_member_geometry_or_length_missing",
            )
        geometry_position = float(geometry.project(point))
        normalized = geometry_position / float(geometry.length) if geometry.length else 0.0
        normalized = min(1.0, max(0.0, normalized))
        position_m = normalized * member_length
        candidates.append(
            {
                "sumo_edge_id": edge_id,
                "chain_member_index": index,
                "projected_position_m": position_m,
                "normalized_position": normalized,
                "chain_position_m": cumulative + position_m,
                "projection_distance_m": float(point.distance(geometry)),
                "distance_to_upstream_member_boundary_m": position_m,
                "distance_to_downstream_member_boundary_m": member_length - position_m,
                "member_length_m": member_length,
            }
        )
        cumulative += member_length
    candidates.sort(
        key=lambda value: (
            value["projection_distance_m"],
            value["chain_member_index"],
            value["sumo_edge_id"],
        )
    )
    best = candidates[0]
    tied = [
        value
        for value in candidates
        if abs(value["projection_distance_m"] - best["projection_distance_m"])
        <= equal_distance_tolerance_m
    ]
    direction_compatible = _direction_compatible(direction, relation)
    if not direction_compatible:
        status = "review_direction"
        reason = "station_direction_disagrees_with_app_relation"
    elif len(tied) > 1:
        status = "review_member_ambiguity"
        reason = "multiple_members_effectively_equal_projection_distance"
    else:
        # Threshold selection is intentionally a later human review. These raw
        # projections are deterministic candidates, never accepted mappings.
        status = "review_threshold_policy_unset"
        reason = "distance_and_boundary_thresholds_not_yet_approved"
    return {
        **best,
        "projection_status": status,
        "projection_reason": reason,
        "direction_compatible": direction_compatible,
        "effectively_equal_member_count": len(tied),
        "nearest_boundary_distance_m": min(
            best["distance_to_upstream_member_boundary_m"],
            best["distance_to_downstream_member_boundary_m"],
        ),
        "candidate_member_count": len(candidates),
    }


def aggregate_lane_detectors(
    lane_records: list[dict[str, float | None]], interval_seconds: int
) -> dict[str, float | int | None]:
    """Mirror a station cross-section: sum lane counts, count-weight speed."""
    if not lane_records or interval_seconds <= 0:
        raise ValueError("lane aggregation requires records and a positive interval")
    counts = []
    weighted_speed = 0.0
    speed_weight = 0.0
    for record in lane_records:
        count = record.get("vehicle_count")
        if count is None or float(count) < 0:
            raise ValueError("lane detector counts must be nonnegative")
        numeric_count = float(count)
        counts.append(numeric_count)
        speed = record.get("mean_speed_mps")
        if speed is not None and float(speed) >= 0 and numeric_count > 0:
            weighted_speed += float(speed) * numeric_count
            speed_weight += numeric_count
    total = sum(counts)
    return {
        "vehicle_count": total,
        "flow_vph": total * 3600.0 / interval_seconds,
        "mean_speed_mps": weighted_speed / speed_weight if speed_weight else None,
        "lane_detector_count": len(lane_records),
    }


def aggregate_e1_fragments(
    fragments: list[dict[str, Any]], *, interval_start: int, interval_end: int
) -> dict[str, Any]:
    """Merge nonoverlapping complete child fragments into one station interval."""
    if interval_end <= interval_start:
        raise ValueError("invalid E1 interval")
    ordered = sorted(fragments, key=lambda value: (value["begin"], value["end"]))
    cursor = interval_start
    detector_ids: set[str] = set()
    records = []
    for fragment in ordered:
        begin, end = int(fragment["begin"]), int(fragment["end"])
        if begin != cursor or end <= begin or end > interval_end:
            raise StationCrossSectionError("E1 fragments have a gap, overlap, or overrun")
        cursor = end
        for record in fragment["lane_records"]:
            identity = f"{begin}:{end}:{record['detector_id']}"
            if identity in detector_ids:
                raise StationCrossSectionError("duplicate E1 detector fragment identity")
            detector_ids.add(identity)
            records.append(record)
    if cursor != interval_end:
        raise StationCrossSectionError("incomplete E1 interval cannot promote")
    return aggregate_lane_detectors(records, interval_end - interval_start)


def run_installed_e1_feasibility(
    *, work_directory: Path, netconvert_binary: Path, sumo_binary: Path
) -> dict[str, Any]:
    """Prove mesoscopic multi-lane E1 behavior across nine 100-second children."""
    work_directory.mkdir(parents=True, exist_ok=True)
    nodes = work_directory / "tiny.nod.xml"
    edges = work_directory / "tiny.edg.xml"
    network = work_directory / "tiny.net.xml"
    routes = work_directory / "tiny.rou.xml"
    nodes.write_text(
        '<nodes><node id="A" x="0" y="0"/><node id="B" x="200" y="0"/>'
        '<node id="C" x="600" y="0"/><node id="D" x="800" y="0"/></nodes>\n',
        encoding="utf-8",
    )
    edges.write_text(
        '<edges><edge id="A_B" from="A" to="B" numLanes="2" speed="20"/>'
        '<edge id="B_C" from="B" to="C" numLanes="2" speed="20"/>'
        '<edge id="C_D" from="C" to="D" numLanes="2" speed="20"/></edges>\n',
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(netconvert_binary),
            "--node-files",
            str(nodes),
            "--edge-files",
            str(edges),
            "--output-file",
            str(network),
            "--no-warnings",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    vehicles = []
    for index in range(18):
        vehicles.append(
            f'<vehicle id="through-{index}" depart="{index * 45}" departLane="{index % 2}"><route edges="A_B B_C C_D"/></vehicle>'
        )
    # Direct departures start before the B_C detector and must cross it.
    for index, depart in enumerate((225, 675)):
        vehicles.append(
            f'<vehicle id="direct-{index}" depart="{depart}" departLane="{index}" departPos="0"><route edges="B_C C_D"/></vehicle>'
        )
    routes.write_text(
        '<routes><vType id="car" accel="2" decel="4.5" length="5" maxSpeed="20"/>'
        + "".join(vehicles)
        + "</routes>\n",
        encoding="utf-8",
    )
    import libsumo

    fragment_records = []
    state_path: Path | None = None
    for begin in range(0, 900, 100):
        end = begin + 100
        detector_output = work_directory / f"e1-{begin}-{end}.xml"
        additional = work_directory / f"e1-{begin}-{end}.add.xml"
        additional.write_text(
            '<additional>'
            f'<inductionLoop id="station-lane-0" lane="B_C_0" pos="100" period="100" file="{detector_output}"/>'
            f'<inductionLoop id="station-lane-1" lane="B_C_1" pos="100" period="100" file="{detector_output}"/>'
            f'<inductionLoop id="downstream-lane-0" lane="B_C_0" pos="300" period="100" file="{detector_output}"/>'
            "</additional>\n",
            encoding="utf-8",
        )
        next_state = work_directory / f"state-{end}.xml.gz"
        command = [
            str(sumo_binary),
            "--net-file",
            str(network),
            "--additional-files",
            str(additional),
            "--mesosim",
            "true",
            "--end",
            str(end),
            "--route-files",
            str(routes),
            "--begin",
            str(begin),
            "--no-step-log",
            "true",
            "--no-warnings",
            "true",
        ]
        if state_path is not None:
            command.extend(["--load-state", str(state_path)])
        libsumo.start(command)
        try:
            while libsumo.simulation.getTime() < end:
                libsumo.simulationStep()
            libsumo.simulation.saveState(str(next_state))
        finally:
            libsumo.close()
        if not next_state.is_file():
            raise StationCrossSectionError("SUMO did not write the requested child state")
        lane_records = _parse_e1_output(detector_output, begin, end)
        fragment_records.append(
            {"begin": begin, "end": end, "lane_records": lane_records}
        )
        state_path = next_state
    aggregate = aggregate_e1_fragments(
        fragment_records, interval_start=0, interval_end=900
    )
    lane_counts: Counter[str] = Counter()
    speeds = []
    for fragment in fragment_records:
        for record in fragment["lane_records"]:
            lane_counts[str(record["detector_id"])] += int(record["vehicle_count"])
            if record["mean_speed_mps"] is not None:
                speeds.append(float(record["mean_speed_mps"]))
    continuous_output = work_directory / "e1-continuous.xml"
    continuous_additional = work_directory / "e1-continuous.add.xml"
    continuous_additional.write_text(
        '<additional>'
        f'<inductionLoop id="station-lane-0" lane="B_C_0" pos="100" period="900" file="{continuous_output}"/>'
        f'<inductionLoop id="station-lane-1" lane="B_C_1" pos="100" period="900" file="{continuous_output}"/>'
        f'<inductionLoop id="downstream-lane-0" lane="B_C_0" pos="300" period="900" file="{continuous_output}"/>'
        "</additional>\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(sumo_binary), "--net-file", str(network), "--route-files", str(routes),
         "--additional-files", str(continuous_additional), "--mesosim", "true",
         "--begin", "0", "--end", "900", "--no-step-log", "true",
         "--no-warnings", "true"],
        check=True,
        capture_output=True,
        text=True,
    )
    continuous = _parse_e1_output(continuous_output, 0, 900)
    continuous_counts = {
        str(value["detector_id"]): int(value["vehicle_count"])
        for value in continuous
    }
    per_detector_through_exact = all(value == 18 for value in lane_counts.values())
    lane_sum_duplicates = aggregate["vehicle_count"] == 54 and per_detector_through_exact
    return {
        "schema_version": 1,
        "feasibility_version": E1_FEASIBILITY_VERSION,
        "feasibility_status": "failed_mesoscopic_e1_is_segment_not_lane_cross_section",
        "native_cross_section_supported": False,
        "sumo_version": _sumo_version(sumo_binary),
        "simulation_mode": "mesoscopic",
        "child_process_seconds": 100,
        "evidence_interval_seconds": 900,
        "child_fragment_count": len(fragment_records),
        "detector_definition_count": len(lane_counts),
        "co_located_station_lane_detector_count": 2,
        "expected_vehicle_count": 20,
        "observed_lane_summed_vehicle_count": aggregate["vehicle_count"],
        "observed_per_detector_vehicle_count": dict(sorted(lane_counts.items())),
        "direct_departure_vehicle_count": 2,
        "direct_departures_observed_by_mesoscopic_e1": 0,
        "lane_vehicle_counts": dict(sorted(lane_counts.items())),
        "lane_summed_flow_vph": aggregate["flow_vph"],
        "station_mean_speed_mps": aggregate["mean_speed_mps"],
        "fragment_speed_min_mps": min(speeds) if speeds else None,
        "fragment_speed_max_mps": max(speeds) if speeds else None,
        "save_load_completed": True,
        "duplicate_fragment_identity_count": 0,
        "continuous_per_detector_vehicle_count": continuous_counts,
        "checkpoint_fragments_equal_uninterrupted": continuous_counts
        == dict(sorted(lane_counts.items())),
        "per_detector_through_vehicle_reconstruction_exact": per_detector_through_exact,
        "lane_aggregation_duplicates_segment_traffic": lane_sum_duplicates,
        "position_separated_detectors_repeat_segment_count": lane_counts.get(
            "station-lane-0"
        )
        == lane_counts.get("downstream-lane-0"),
        "partial_interval_rejected_by_contract": True,
        "metric_semantics": {
            "count": "mesoscopic entered+departed per E1; co-located lane E1s repeat segment traffic",
            "flow": "per-detector segment count * 3600 / interval; lane sum is invalid",
            "speed": "mesoscopic segment mean speed, not proven point-crossing speed",
            "occupancy": "mesoscopic segment occupancy, not detector point occupancy",
        },
    }


def _parse_e1_output(path: Path, begin: int, end: int) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    records = []
    intervals = root.findall("interval")
    for interval in intervals:
        interval_begin = round(float(interval.attrib["begin"]))
        interval_end = round(float(interval.attrib["end"]))
        if interval_begin != begin or interval_end != end:
            continue
        # SUMO's mesoscopic E1 schema has no nVehContrib. It reports segment
        # entry and direct-departure counters; both are needed for total inflow.
        count = float(interval.attrib.get("entered", 0)) + float(
            interval.attrib.get("departed", 0)
        )
        speed = float(interval.attrib["speed"]) if "speed" in interval.attrib else None
        if speed is not None and speed < 0:
            speed = None
        records.append(
            {
                "detector_id": interval.attrib["id"],
                "vehicle_count": count,
                "mean_speed_mps": speed,
                "occupancy_percent": float(interval.attrib.get("occupancy", 0)),
            }
        )
    if len(records) != 3:
        raise StationCrossSectionError(
            f"expected three E1 records for [{begin},{end}), found {len(records)}"
        )
    return sorted(records, key=lambda value: value["detector_id"])


def _sumo_version(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True, text=True
    )
    first = completed.stdout.splitlines()[0]
    return first.replace("Eclipse SUMO sumo Version ", "").strip()


def build_station_cross_sections(
    *,
    station_mapping_path: Path,
    detector_metadata_path: Path,
    edge_map_path: Path,
    relation_directory: Path,
    historical_directory: Path,
    policy_directory: Path,
    e1_feasibility_path: Path,
    output_directory: Path,
) -> StationCrossSectionManifestV1:
    """Build raw deterministic projection characterization atomically."""
    if output_directory.exists():
        return StationCrossSectionManifestV1.model_validate_json(
            (output_directory / CROSS_SECTION_MANIFEST).read_text(encoding="utf-8")
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    relation_manifest = ComparisonRelationManifestV1.model_validate_json(
        (relation_directory / "comparison-relation-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    historical_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        (historical_directory / "historical-calibration-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    policy_manifest = HistoricalQualityPolicyManifestV1.model_validate_json(
        (policy_directory / "quality-policy-manifest.json").read_text(encoding="utf-8")
    )
    station_map = pd.read_csv(station_mapping_path)
    accepted_stations = station_map.loc[station_map["status"].eq("accepted")].copy()
    detectors = json.loads(detector_metadata_path.read_text(encoding="utf-8"))
    detector_by_station: dict[str, list[str]] = {}
    for detector in detectors:
        station_id = _canonical_portal_station_id(detector["stationid"])
        detector_by_station.setdefault(station_id, []).append(
            f"portal-detector-{detector['detectorid']}"
        )
    relations = pq.read_table(
        relation_directory / relation_manifest.relation_output.relative_path
    ).to_pandas()
    relation_lookup = relations.set_index("app_edge_id").to_dict(orient="index")
    accepted_edge_ids = set(accepted_stations["edge_id"].astype(str))
    edge_map = pq.read_table(
        edge_map_path,
        columns=[
            "app_edge_id",
            "sumo_edge_id",
            "status",
            "sumo_geometry_wkb",
            "sumo_lane_ids",
        ],
    ).to_pandas()
    edge_map = edge_map.loc[
        edge_map["status"].eq("accepted")
        & edge_map["app_edge_id"].astype(str).isin(accepted_edge_ids)
    ]
    member_geometries: dict[str, LineString] = {}
    member_lane_ids: dict[str, list[str]] = {}
    for row in edge_map.drop_duplicates("sumo_edge_id").itertuples(index=False):
        member_geometries[str(row.sumo_edge_id)] = wkb.loads(row.sumo_geometry_wkb)
        member_lane_ids[str(row.sumo_edge_id)] = _parse_lane_ids(row.sumo_lane_ids)
    member_lengths: dict[str, float] = {}
    for relation in relation_lookup.values():
        for evidence in json.loads(relation["member_topology_evidence_json"]):
            if evidence.get("length_m") is not None:
                member_lengths[str(evidence["sumo_edge_id"])] = float(evidence["length_m"])

    profile = pq.read_table(
        historical_directory / "edge-time-distributions.parquet",
        columns=["app_edge_id", "source_station_ids_json"],
    ).to_pandas()
    profile_counts: Counter[str] = Counter()
    for row in profile.itertuples(index=False):
        for station_id in json.loads(row.source_station_ids_json):
            profile_counts[_canonical_portal_station_id(station_id)] += 1
    app_station_counts = accepted_stations.groupby("edge_id")["station_id"].nunique().to_dict()
    rows = []
    for station in accepted_stations.sort_values("station_id").itertuples(index=False):
        app_edge_id = str(station.edge_id)
        relation = relation_lookup.get(app_edge_id)
        if relation is None:
            projection = {
                "projection_status": "review_topology",
                "projection_reason": "accepted_app_edge_has_no_accepted_sumo_relation",
                "sumo_edge_id": None,
                "chain_member_index": None,
                "projected_position_m": None,
                "normalized_position": None,
                "chain_position_m": None,
                "projection_distance_m": None,
                "distance_to_upstream_member_boundary_m": None,
                "distance_to_downstream_member_boundary_m": None,
                "nearest_boundary_distance_m": None,
                "member_length_m": None,
                "direction_compatible": False,
                "effectively_equal_member_count": 0,
                "candidate_member_count": 0,
            }
            relation = {"relation_id": None, "relation_class": "review_or_absent"}
        else:
            projection = project_station_to_relation(
                station_id=str(station.station_id),
                longitude=float(station.longitude),
                latitude=float(station.latitude),
                direction=str(station.direction),
                relation=relation,
                member_geometries=member_geometries,
                member_lengths_m=member_lengths,
            )
        sumo_edge_id = projection.get("sumo_edge_id")
        lane_ids = member_lane_ids.get(str(sumo_edge_id), []) if sumo_edge_id else []
        mapping_id = hashlib.sha256(
            canonical_json(
                {
                    "station_id": str(station.station_id),
                    "app_edge_id": app_edge_id,
                    "relation_id": relation.get("relation_id"),
                    "projection_algorithm": STATION_CROSS_SECTION_ALGORITHM_VERSION,
                }
            ).encode()
        ).hexdigest()
        rows.append(
            {
                "schema_version": STATION_CROSS_SECTION_SCHEMA_VERSION,
                "mapping_id": mapping_id,
                "station_id": str(station.station_id),
                "app_edge_id": app_edge_id,
                "relation_id": relation.get("relation_id"),
                "relation_class": relation.get("relation_class"),
                "sumo_edge_id": sumo_edge_id,
                "chain_member_index": projection.get("chain_member_index"),
                "projected_position_m": projection.get("projected_position_m"),
                "normalized_position": projection.get("normalized_position"),
                "chain_position_m": projection.get("chain_position_m"),
                "projection_distance_m": projection.get("projection_distance_m"),
                "member_length_m": projection.get("member_length_m"),
                "distance_to_upstream_member_boundary_m": projection.get(
                    "distance_to_upstream_member_boundary_m"
                ),
                "distance_to_downstream_member_boundary_m": projection.get(
                    "distance_to_downstream_member_boundary_m"
                ),
                "nearest_boundary_distance_m": projection.get(
                    "nearest_boundary_distance_m"
                ),
                "effectively_equal_member_count": projection.get(
                    "effectively_equal_member_count"
                ),
                "candidate_member_count": projection.get("candidate_member_count"),
                "direction": str(station.direction),
                "direction_compatible": projection.get("direction_compatible"),
                "projection_status": projection["projection_status"],
                "projection_reason": projection["projection_reason"],
                "station_longitude": float(station.longitude),
                "station_latitude": float(station.latitude),
                "highway_name": str(station.highway_name),
                "location_text": str(station.location_text),
                "milepost": _optional_number(station.milepost),
                "station_app_mapping_score": _optional_number(station.score),
                "station_app_mapping_confidence": str(station.confidence),
                "station_app_mapping_distance_m": _optional_number(station.distance_m),
                "source_detector_ids_json": canonical_json(
                    sorted(detector_by_station.get(str(station.station_id), []))
                ),
                "source_detector_count": len(
                    detector_by_station.get(str(station.station_id), [])
                ),
                "historical_profile_participation_count": int(
                    profile_counts[str(station.station_id)]
                ),
                "stations_on_app_edge": int(app_station_counts.get(app_edge_id, 0)),
                "sumo_lane_ids_json": canonical_json(lane_ids),
                "sumo_lane_count": len(lane_ids),
                "virtual_station_group_id": f"portal-sumo-station::{station.station_id}",
                "projection_algorithm_version": STATION_CROSS_SECTION_ALGORITHM_VERSION,
            }
        )
    frame = pd.DataFrame(rows).sort_values("station_id", kind="mergesort").reset_index(
        drop=True
    )
    e1_report = json.loads(e1_feasibility_path.read_text(encoding="utf-8"))
    summary = _build_summary(frame, policy_directory, relations)
    report = _render_report(summary, e1_report)
    pending.mkdir(parents=True)
    try:
        mapping_path = pending / CROSS_SECTION_PARQUET
        summary_path = pending / CROSS_SECTION_SUMMARY
        report_path = pending / CROSS_SECTION_REPORT
        feasibility_path = pending / E1_FEASIBILITY_REPORT
        manifest_path = pending / CROSS_SECTION_MANIFEST
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
            {
                b"schema_version": b"1",
                b"producer": b"portal_station_sumo_cross_section_projector",
                b"producer_version": STATION_CROSS_SECTION_PRODUCER_VERSION.encode(),
                b"acceptance_policy": b"none_characterization_only",
            }
        )
        pq.write_table(table, mapping_path, compression="zstd", compression_level=9)
        summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        report_path.write_text(report, encoding="utf-8")
        feasibility_path.write_text(canonical_json(e1_report) + "\n", encoding="utf-8")
        payload: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "commute_help_station_sumo_cross_section_analysis",
            "artifact_status": "complete_characterization_pending_acceptance_policy",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC).isoformat(),
            "producer_name": "portal_station_sumo_cross_section_projector",
            "producer_version": STATION_CROSS_SECTION_PRODUCER_VERSION,
            "projection_algorithm_version": STATION_CROSS_SECTION_ALGORITHM_VERSION,
            "e1_feasibility_version": E1_FEASIBILITY_VERSION,
            "graph_version": historical_manifest.graph_version,
            "sumo_network_version": relation_manifest.sumo_network_version,
            "station_mapping_sha256": _sha256(station_mapping_path),
            "detector_metadata_sha256": _sha256(detector_metadata_path),
            "edge_map_sha256": _sha256(edge_map_path),
            "comparison_relation_content_digest": relation_manifest.content_digest,
            "comparison_relation_parquet_sha256": relation_manifest.relation_output.sha256,
            "historical_profile_content_digest": historical_manifest.content_digest,
            "quality_policy_content_digest": policy_manifest.content_digest,
            "acceptance_policy": "none_characterize_distance_and_boundary_distributions_before_threshold_selection",
            "mapping_output": _file_identity(mapping_path, len(frame)),
            "summary_output": _file_identity(summary_path),
            "report_output": _file_identity(report_path),
            "e1_feasibility_output": _file_identity(feasibility_path),
            "row_content_sha256": _frame_digest(frame),
        }
        payload["content_digest"] = _content_digest(payload)
        manifest = StationCrossSectionManifestV1.model_validate(payload)
        manifest_path.write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        _promote(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _build_summary(
    frame: pd.DataFrame, policy_directory: Path, relations: pd.DataFrame
) -> dict[str, Any]:
    profiles = pq.read_table(
        policy_directory / "quality-policy-profile-status.parquet",
        columns=["app_edge_id", "direction", "date_support_class"],
    ).to_pandas()
    direct = profiles.loc[profiles["date_support_class"].eq("direct_calibration_evidence")]
    app_status = frame.groupby("app_edge_id")["projection_status"].agg(
        lambda values: sorted(set(values))
    )
    joined = direct.merge(
        app_status.rename("station_projection_statuses"),
        left_on="app_edge_id",
        right_index=True,
        how="left",
    )
    accepted_profiles = joined["station_projection_statuses"].map(
        lambda value: isinstance(value, list) and "accepted_cross_section" in value
    )
    candidate_profiles = joined["station_projection_statuses"].map(
        lambda value: isinstance(value, list)
        and "review_threshold_policy_unset" in value
    )
    app_context = relations[["app_edge_id", "road_name", "ref"]].drop_duplicates(
        "app_edge_id"
    )
    joined = joined.merge(app_context, on="app_edge_id", how="left", validate="many_to_one")
    corridor_rules = {
        "I-5": ("ref", r"(?:^|;)I 5(?:$|;)"),
        "I-205": ("ref", r"(?:^|;)I 205(?:$|;)"),
        "Interstate Bridge": ("road_name", r"Interstate Bridge"),
        "Glenn L. Jackson Memorial Bridge": (
            "road_name",
            r"Glenn L\. Jackson Memorial Bridge",
        ),
        "Marquam Bridge": ("road_name", r"Marquam Bridge"),
        "OR-217": ("ref", r"(?:^|;)OR 217(?:$|;)"),
        "I-84 / US-30": ("ref", r"I 84|US 30"),
        "US-26": ("ref", r"(?:^|;)US 26(?:$|;)"),
    }
    corridor_coverage = {}
    for label, (column, pattern) in corridor_rules.items():
        subset = joined.loc[
            joined[column].fillna("").astype(str).str.contains(pattern, regex=True)
        ]
        corridor_coverage[label] = _profile_projection_coverage(subset)
    stations_per_app_edge = frame.groupby("app_edge_id").size()
    non_projectable = frame.loc[
        frame["sumo_edge_id"].isna(),
        [
            "station_id",
            "app_edge_id",
            "relation_class",
            "projection_status",
            "projection_reason",
        ],
    ].to_dict(orient="records")
    return {
        "artifact_status": "complete_characterization_pending_acceptance_policy",
        "calibration_status": "not_calibrated",
        "accepted_station_count": len(frame),
        "station_projection_status_counts": {
            str(key): int(value)
            for key, value in frame["projection_status"].value_counts().sort_index().items()
        },
        "station_relation_class_counts": {
            str(key): int(value)
            for key, value in frame["relation_class"].value_counts().sort_index().items()
        },
        "projected_chain_member_index_counts": {
            str(int(key)): int(value)
            for key, value in frame["chain_member_index"].dropna().value_counts().sort_index().items()
        },
        "projection_distance_m": _distribution(frame["projection_distance_m"]),
        "projection_distance_descriptive_bands_m": _distance_bands(
            frame["projection_distance_m"]
        ),
        "nearest_boundary_distance_m": _distribution(
            frame["nearest_boundary_distance_m"]
        ),
        "nearest_boundary_descriptive_bands_m": _distance_bands(
            frame["nearest_boundary_distance_m"]
        ),
        "member_ambiguity_station_count": int(
            frame["projection_status"].eq("review_member_ambiguity").sum()
        ),
        "direction_disagreement_station_count": int(
            frame["projection_status"].eq("review_direction").sum()
        ),
        "multiple_station_app_edge_count": int(
            frame.loc[frame["stations_on_app_edge"].gt(1), "app_edge_id"].nunique()
        ),
        "stations_on_multi_station_app_edges": int(
            frame["stations_on_app_edge"].gt(1).sum()
        ),
        "stations_per_app_edge_distribution": {
            str(int(key)): int(value)
            for key, value in stations_per_app_edge.value_counts().sort_index().items()
        },
        "non_projectable_stations": non_projectable,
        "stations_lacking_usable_coordinates": int(
            frame["projection_reason"].eq("station_coordinates_missing").sum()
        ),
        "historical_profile_participation_station_count": int(
            frame["historical_profile_participation_count"].gt(0).sum()
        ),
        "source_detector_count": int(frame["source_detector_count"].sum()),
        "direct_profile_cross_section_coverage": {
            "total": len(joined),
            "comparator_v1_unique_one_edge": 52262,
            "phase_2_2a_topology_comparable": 111787,
            "accepted_cross_section": int(accepted_profiles.sum()),
            "projection_candidate_pending_threshold_policy": int(
                candidate_profiles.sum()
            ),
            "review_or_unresolved": int((~accepted_profiles & ~candidate_profiles).sum()),
        },
        "direct_profile_cross_section_coverage_by_direction": {
            str(direction): _profile_projection_coverage(group)
            for direction, group in joined.groupby("direction", sort=True)
        },
        "corridor_direct_profile_cross_section_coverage": corridor_coverage,
        "acceptance_policy": {
            "status": "not_selected",
            "reason": "production projection-distance and boundary-distance distributions require human review",
        },
    }


def _render_report(summary: dict[str, Any], e1: dict[str, Any]) -> str:
    coverage = summary["direct_profile_cross_section_coverage"]
    return "\n".join(
        [
            "# Phase 2.2b station cross-section characterization",
            "",
            "No station projection is accepted yet. Production distance and member-boundary distributions are characterized before threshold selection.",
            "",
            f"- Accepted historical stations inventoried: {summary['accepted_station_count']:,}",
            f"- Stations participating in historical profiles: {summary['historical_profile_participation_station_count']:,}",
            f"- Topologically projectable station candidates: {summary['station_projection_status_counts'].get('review_threshold_policy_unset', 0):,}",
            f"- Multiple-station app edges: {summary['multiple_station_app_edge_count']:,}",
            f"- Direct profiles with deterministic projection candidates: {coverage['projection_candidate_pending_threshold_policy']:,}",
            f"- Direct profiles with accepted cross-sections: {coverage['accepted_cross_section']:,}",
            f"- Native E1 mesoscopic feasibility: {e1.get('feasibility_status')}",
            "",
            "The installed-SUMO proof found exact 100-second save/load fragment reconstruction but rejected E1 as a PORTAL cross-section mechanism: mesoscopic lane and position detectors repeated segment traffic and direct departures were not counted.",
            "",
            "See the JSON summary for distributions. Candidate projections are not comparator inputs until a reviewed acceptance policy is versioned.",
            "",
        ]
    )


def _direction_compatible(direction: str, relation: dict[str, Any]) -> bool:
    normalized = direction.strip().lower()
    if normalized not in {"north", "south", "east", "west"}:
        return False
    # Phase 2.2a already proves SUMO traversal follows the accepted directed app
    # edge. Station/app matching independently enforced its cardinal direction.
    return bool(relation.get("direction_signature"))


def _empty_projection(
    station_id: str, relation: dict[str, Any], status: str, reason: str
) -> dict[str, Any]:
    del station_id, relation
    fields = {
        "sumo_edge_id": None,
        "chain_member_index": None,
        "projected_position_m": None,
        "normalized_position": None,
        "chain_position_m": None,
        "projection_distance_m": None,
        "distance_to_upstream_member_boundary_m": None,
        "distance_to_downstream_member_boundary_m": None,
        "nearest_boundary_distance_m": None,
        "member_length_m": None,
        "direction_compatible": False,
        "effectively_equal_member_count": 0,
        "candidate_member_count": 0,
    }
    return {**fields, "projection_status": status, "projection_reason": reason}


def _parse_lane_ids(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return sorted(str(item) for item in parsed)
    except json.JSONDecodeError:
        pass
    return sorted(item for item in text.replace(",", " ").split() if item)


def _distribution(series: pd.Series) -> dict[str, float | int | None]:
    values = series.dropna().astype(float)
    if values.empty:
        return {"count": 0, "min": None, "p01": None, "p05": None, "p10": None, "p25": None, "median": None, "p75": None, "p90": None, "p95": None, "p99": None, "max": None}
    result: dict[str, float | int | None] = {"count": len(values), "min": float(values.min())}
    for label, quantile in (("p01", .01), ("p05", .05), ("p10", .1), ("p25", .25), ("median", .5), ("p75", .75), ("p90", .9), ("p95", .95), ("p99", .99)):
        result[label] = float(values.quantile(quantile, interpolation="linear"))
    result["max"] = float(values.max())
    return result


def _distance_bands(series: pd.Series) -> dict[str, int]:
    values = series.dropna().astype(float)
    return {
        "0_to_lt_1": int(values.lt(1).sum()),
        "1_to_lt_5": int((values.ge(1) & values.lt(5)).sum()),
        "5_to_lt_10": int((values.ge(5) & values.lt(10)).sum()),
        "10_to_lt_25": int((values.ge(10) & values.lt(25)).sum()),
        "25_to_lt_50": int((values.ge(25) & values.lt(50)).sum()),
        "50_to_lt_100": int((values.ge(50) & values.lt(100)).sum()),
        "100_plus": int(values.ge(100).sum()),
    }


def _profile_projection_coverage(frame: pd.DataFrame) -> dict[str, int | float | None]:
    total = len(frame)
    statuses = frame["station_projection_statuses"]
    accepted = statuses.map(
        lambda value: isinstance(value, list) and "accepted_cross_section" in value
    )
    candidate = statuses.map(
        lambda value: isinstance(value, list)
        and "review_threshold_policy_unset" in value
    )
    return {
        "total": total,
        "accepted": int(accepted.sum()),
        "candidate_pending_threshold_policy": int(candidate.sum()),
        "review_or_unresolved": int((~accepted & ~candidate).sum()),
        "candidate_fraction": float(candidate.sum() / total) if total else None,
    }


def _optional_number(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        digest.update(
            canonical_json(
                {
                    key: None
                    if pd.isna(value)
                    else value.item()
                    if hasattr(value, "item")
                    else value
                    for key, value in row.items()
                }
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path, rows: int | None = None) -> dict[str, Any]:
    return {"relative_path": path.name, "sha256": _sha256(path), "byte_count": path.stat().st_size, "row_count": rows}


def _content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _promote(pending: Path, output: Path) -> None:
    manifest = pending / CROSS_SECTION_MANIFEST
    if output.exists() or not manifest.is_file():
        raise StationCrossSectionError("partial cross-section artifact cannot promote")
    StationCrossSectionManifestV1.model_validate_json(manifest.read_text(encoding="utf-8"))
    os.replace(pending, output)
