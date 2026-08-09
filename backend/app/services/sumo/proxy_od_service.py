"""Compile an all-local, schedule-aware proxy OD snapshot for SUMO."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from backend.app.services.traffic_schedule_service import (
    LOCAL_TIMEZONE,
    TrafficScheduleService,
)


class ProxyOdError(RuntimeError):
    """Local artifacts cannot produce an honest proxy demand snapshot."""


def compile_proxy_od_snapshot(
    *,
    background_seed_directory: Path,
    nodes_path: Path,
    graph_manifest_path: Path,
    schedule_service: TrafficScheduleService,
    departure_time: datetime,
    duration_minutes: int,
    output_directory: Path,
    source_version: str,
    log: Any = print,
) -> dict[str, Any]:
    if departure_time.tzinfo is None:
        raise ProxyOdError("Proxy demand departure time must include a timezone")
    if duration_minutes < 1 or duration_minutes > 360:
        raise ProxyOdError("Proxy demand duration must be between 1 and 360 minutes")
    if output_directory.exists():
        raise ProxyOdError("Proxy OD output already exists; choose a new demand version")

    od_seed_path = background_seed_directory / "od-demand-seeds.parquet"
    seed_report_path = background_seed_directory / "validation-report.json"
    for path in (od_seed_path, seed_report_path, nodes_path, graph_manifest_path):
        if not path.is_file():
            raise ProxyOdError(f"Required local proxy input is missing: {path.name}")
    graph_manifest = _read_json(graph_manifest_path)
    seed_report = _read_json(seed_report_path)
    graph_version = str(graph_manifest["graph_version"])
    if str(seed_report.get("graph_version")) != graph_version:
        raise ProxyOdError("Background seed and active graph versions differ")
    if str(seed_report.get("evidence_level")) != "modeled_uncalibrated":
        raise ProxyOdError("Background seed evidence level is not modeled_uncalibrated")

    log("LOAD local detector-fitted proxy OD paths")
    seeds = pd.read_parquet(od_seed_path)
    required = {
        "pair_id",
        "origin_zone",
        "destination_zone",
        "origin_node_id",
        "destination_node_id",
        "am_demand_vph",
        "pm_demand_vph",
    }
    missing = sorted(required - set(seeds.columns))
    if missing:
        raise ProxyOdError("Proxy OD seed is missing: " + ", ".join(missing))
    if seeds.empty or seeds["pair_id"].duplicated().any():
        raise ProxyOdError("Proxy OD seed must contain unique nonempty pairs")

    local_departure = departure_time.astimezone(LOCAL_TIMEZONE)
    schedule = _schedule_scaling(schedule_service, local_departure)
    pm_weight = proxy_pm_weight(
        local_departure.hour * 60
        + local_departure.minute
        + local_departure.second / 60.0
    )
    am = seeds["am_demand_vph"].fillna(0).clip(lower=0).to_numpy(dtype=float)
    pm = seeds["pm_demand_vph"].fillna(0).clip(lower=0).to_numpy(dtype=float)
    am_total = float(am.sum())
    pm_total = float(pm.sum())
    if am_total <= 0 or pm_total <= 0:
        raise ProxyOdError("Proxy OD AM and PM totals must both be positive")
    distribution = (1 - pm_weight) * am / am_total + pm_weight * pm / pm_total
    base_total = (1 - pm_weight) * am_total + pm_weight * pm_total
    anchor_total = (
        (1 - pm_weight) * schedule["am_anchor_volume"]
        + pm_weight * schedule["pm_anchor_volume"]
    )
    schedule_factor = schedule["requested_volume"] / max(anchor_total, 1.0)
    target_vph = max(0.0, base_total * schedule_factor)
    rates = distribution * target_vph
    trips = rates * duration_minutes / 60.0

    od = pd.DataFrame(
        {
            "normalized_schema_version": 1,
            "source": "local_portal_proxy",
            "campaign_id": source_version,
            "model_name": "Local detector-fitted proxy OD",
            "model_version": str(seed_report["model_version"]),
            "base_year": local_departure.year,
            "period": "proxy_snapshot",
            "origin_zone_id": seeds["origin_zone"].astype(str),
            "destination_zone_id": seeds["destination_zone"].astype(str),
            "vehicle_class": "sov",
            "vehicle_trips": trips,
            "vehicle_trip_rate_vph": rates,
            "period_duration_minutes": float(duration_minutes),
            "source_trip_measure": "modeled_vehicle_trips",
            "source_value_basis": "schedule_scaled_proxy_vph",
            "quality_flag": "modeled_uncalibrated_local_proxy",
        }
    )
    zones = _proxy_zones(seeds, nodes_path)
    output_directory.mkdir(parents=True)
    od_path = output_directory / "od-demand.parquet"
    zones_path = output_directory / "zones.parquet"
    _atomic_parquet(od_path, od)
    _atomic_parquet(zones_path, zones)
    evidence_levels = schedule["evidence_levels"]
    report = {
        "schema_version": 1,
        "source_kind": "local_proxy",
        "campaign_id": source_version,
        "agency": "local_portal_osm_proxy",
        "model": {
            "name": "Local detector-fitted proxy OD",
            "version": str(seed_report["model_version"]),
            "base_year": local_departure.year,
            "network_year": local_departure.year,
        },
        "evidence_level": "modeled_uncalibrated",
        "simulation_ready": True,
        "assignment_ready": False,
        "publication_ready": False,
        "requested_at": local_departure.isoformat(),
        "duration_minutes": duration_minutes,
        "graph_version": graph_version,
        "schedule_version": schedule["schedule_version"],
        "schedule_evidence_levels": evidence_levels,
        "pattern": {
            "pm_weight": pm_weight,
            "am_proxy_total_vph": am_total,
            "pm_proxy_total_vph": pm_total,
            "requested_detector_volume": schedule["requested_volume"],
            "am_anchor_detector_volume": schedule["am_anchor_volume"],
            "pm_anchor_detector_volume": schedule["pm_anchor_volume"],
            "schedule_factor": schedule_factor,
            "generated_proxy_total_vph": float(rates.sum()),
            "generated_real_vehicle_trips": float(trips.sum()),
        },
        "quality": {
            "proxy_zone_count": int(len(zones)),
            "proxy_od_pair_count": int(len(od)),
            "seed_status": seed_report["status"],
            "seed_validation": seed_report["periods"],
        },
        "inputs": {
            "od_seed_sha256": _sha256(od_seed_path),
            "seed_report_sha256": _sha256(seed_report_path),
            "nodes_sha256": _sha256(nodes_path),
            "graph_manifest_sha256": _sha256(graph_manifest_path),
            "schedule_sha256": _sha256(schedule_service.schedule_path),
        },
        "license": {
            "terms_reference": "Generated locally from existing OSM network and PORTAL-derived artifacts",
            "internal_model_use_authorized": True,
            "source_data_redistribution": "prohibited",
            "code_publication_status": "allowed",
            "derived_output_publication_status": "prohibited",
        },
        "limitations": [
            "Proxy zones represent network activity, not observed households, jobs, or agency TAZs.",
            "The spatial pattern blends two detector-fitted weekday proxy matrices.",
            "The 24/7 schedule scales regional demand from freeway detector totals; it does not observe neighborhood origins.",
            "Weekend schedule inputs remain modeled_unobserved until weekend observations pass their own gate.",
            "This source is usable for modeled local simulation but must never be labeled historical or agency OD.",
        ],
    }
    _atomic_text(
        output_directory / "demand-source-report.json",
        json.dumps(report, indent=2) + "\n",
    )
    log(
        f"COMPILE proxy_zones={len(zones):,} od_pairs={len(od):,} "
        f"target_vph={rates.sum():,.1f} evidence={','.join(evidence_levels)}"
    )
    return report


def proxy_pm_weight(minute_of_day: float) -> float:
    """Smoothly blend local AM and PM spatial patterns across the full day."""

    minute = float(minute_of_day) % 1440.0
    anchors = np.array([0, 300, 450, 660, 840, 990, 1200, 1440], dtype=float)
    weights = np.array([0.5, 0.15, 0.0, 0.25, 0.75, 1.0, 0.8, 0.5], dtype=float)
    return float(np.interp(minute, anchors, weights))


def _schedule_scaling(
    service: TrafficScheduleService, departure_time: datetime
) -> dict[str, Any]:
    local = departure_time.astimezone(LOCAL_TIMEZONE)
    am_anchor = local.replace(hour=7, minute=30, second=0, microsecond=0)
    pm_anchor = local.replace(hour=16, minute=30, second=0, microsecond=0)
    requested = service.state_at(departure_time)
    morning = service.state_at(am_anchor)
    afternoon = service.state_at(pm_anchor)

    def total(state: dict[str, Any]) -> float:
        return float(sum(float(row["volume_mean"]) for row in state["rows"]))

    return {
        "schedule_version": requested["schedule_version"],
        "requested_volume": total(requested),
        "am_anchor_volume": total(morning),
        "pm_anchor_volume": total(afternoon),
        "evidence_levels": sorted(
            {str(row["evidence_level"]) for row in requested["rows"]}
        ),
    }


def _proxy_zones(seeds: pd.DataFrame, nodes_path: Path) -> gpd.GeoDataFrame:
    endpoints = pd.concat(
        [
            seeds[["origin_zone", "origin_node_id"]].rename(
                columns={"origin_zone": "zone_id", "origin_node_id": "node_id"}
            ),
            seeds[["destination_zone", "destination_node_id"]].rename(
                columns={"destination_zone": "zone_id", "destination_node_id": "node_id"}
            ),
        ],
        ignore_index=True,
    ).drop_duplicates()
    if endpoints["zone_id"].duplicated().any():
        raise ProxyOdError("A proxy zone resolves to multiple graph nodes")
    nodes = gpd.read_parquet(nodes_path)
    node_ids = nodes["osmid"].map(_node_id)
    lookup = gpd.GeoDataFrame(
        {"node_id": node_ids}, geometry=nodes.geometry, crs=nodes.crs
    )
    joined = endpoints.assign(node_id=endpoints["node_id"].map(_node_id)).merge(
        lookup, on="node_id", how="left", validate="many_to_one"
    )
    if joined["geometry"].isna().any():
        raise ProxyOdError("A proxy OD endpoint is missing from the active graph nodes")
    zones = gpd.GeoDataFrame(joined, geometry="geometry", crs=nodes.crs).to_crs(
        "EPSG:32610"
    )
    zones["geometry"] = zones.geometry.buffer(750)
    zones["zone_type"] = "internal"
    return zones[["zone_id", "zone_type", "geometry"]].to_crs("EPSG:4326")


def _node_id(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _atomic_parquet(path: Path, frame: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(value)
        temporary = Path(handle.name)
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProxyOdError(f"Cannot read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ProxyOdError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
