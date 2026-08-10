"""Convert an accepted regional OD package into deterministic SUMO demand."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import geopandas as gpd
import pandas as pd
import sumolib
from shapely import wkb

Log = Callable[[str], None]
DEMAND_SCHEMA_VERSION = 1
CONNECTOR_SCHEMA_VERSION = 1
PROJECTED_CRS = "EPSG:32610"
INTERNAL_CONNECTOR_CLASSES = {
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
}
EXTERNAL_CONNECTOR_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
}
CLASS_PRIORITY = {
    "motorway": 1.00,
    "motorway_link": 0.98,
    "trunk": 0.95,
    "trunk_link": 0.93,
    "primary": 0.90,
    "primary_link": 0.88,
    "secondary": 0.78,
    "secondary_link": 0.75,
    "tertiary": 0.65,
    "tertiary_link": 0.62,
    "unclassified": 0.45,
}
VEHICLE_CLASS_MAP = {
    "sov": ("passenger_sov", "passenger"),
    "single_occupancy_vehicle": ("passenger_sov", "passenger"),
    "all_motor_vehicle": ("passenger_sov", "passenger"),
    "passenger": ("passenger_sov", "passenger"),
    "car": ("passenger_sov", "passenger"),
    "hov": ("passenger_hov", "passenger"),
    "light_truck": ("light_truck", "delivery"),
    "delivery": ("light_truck", "delivery"),
    "commercial": ("light_truck", "delivery"),
    "medium_truck": ("heavy_truck", "truck"),
    "heavy_truck": ("heavy_truck", "truck"),
    "truck": ("heavy_truck", "truck"),
    "bus": ("transit_bus", "bus"),
}
VTYPE_ATTRIBUTES = {
    "passenger_sov": {
        "vClass": "passenger",
        "length": "5.0",
        "minGap": "2.5",
        "maxSpeed": "55.56",
        "color": "0.20,0.55,0.85",
    },
    "passenger_hov": {
        "vClass": "passenger",
        "length": "5.0",
        "minGap": "2.5",
        "maxSpeed": "55.56",
        "color": "0.20,0.75,0.55",
    },
    "light_truck": {
        "vClass": "delivery",
        "length": "7.0",
        "minGap": "3.0",
        "maxSpeed": "44.44",
        "color": "0.85,0.60,0.20",
    },
    "heavy_truck": {
        "vClass": "truck",
        "length": "16.0",
        "minGap": "3.5",
        "maxSpeed": "33.33",
        "color": "0.75,0.35,0.20",
    },
    "transit_bus": {
        "vClass": "bus",
        "length": "12.0",
        "minGap": "3.0",
        "maxSpeed": "33.33",
        "color": "0.60,0.30,0.75",
    },
}


class SumoDemandError(RuntimeError):
    """Demand cannot safely be generated from the supplied artifacts."""


def _load_gateway_connector_config(
    path: Path,
    *,
    network_version: str,
    network_sha256: str,
) -> dict[str, dict[str, str]]:
    """Load checksum-bound deterministic SUMO gateway connectors."""

    payload = _read_json(path)

    if payload.get("schema_version") != 1:
        raise SumoDemandError(
            "Gateway connector artifact must use schema_version 1"
        )

    if str(payload.get("sumo_network_version")) != network_version:
        raise SumoDemandError(
            "Gateway connector artifact does not match "
            "the active SUMO network version"
        )

    if str(payload.get("sumo_network_sha256")) != network_sha256:
        raise SumoDemandError(
            "Gateway connector artifact does not match "
            "the active SUMO network checksum"
        )

    rows = payload.get("gateways")

    if not isinstance(rows, list) or not rows:
        raise SumoDemandError(
            "Gateway connector artifact contains no gateways"
        )

    mappings: dict[str, dict[str, str]] = {}

    for row in rows:
        if not isinstance(row, dict):
            raise SumoDemandError(
                "Gateway connector rows must be JSON objects"
            )

        gateway_id = str(
            row.get("gateway_id") or ""
        ).strip()

        inbound = str(
            row.get("inbound_sumo_edge_id") or ""
        ).strip()

        outbound = str(
            row.get("outbound_sumo_edge_id") or ""
        ).strip()

        evidence = str(
            row.get("evidence") or "reviewed_gateway_connector"
        ).strip()

        if not gateway_id or not inbound or not outbound:
            raise SumoDemandError(
                "Gateway connector rows require gateway_id, "
                "inbound_sumo_edge_id, and outbound_sumo_edge_id"
            )

        if gateway_id in mappings:
            raise SumoDemandError(
                f"Duplicate gateway connector {gateway_id}"
            )

        mappings[gateway_id] = {
            "origin": inbound,
            "destination": outbound,
            "evidence": evidence,
        }

    declared_count = payload.get("gateway_count")

    if declared_count != len(mappings):
        raise SumoDemandError(
            "Gateway connector gateway_count does not "
            "match the configured rows"
        )

    return mappings


def build_sumo_demand(
    *,
    intake_directory: Path,
    network_path: Path,
    network_manifest_path: Path,
    edge_map_path: Path,
    output_directory: Path,
    demand_version: str,
    period: str,
    start_seconds: int,
    sampling_scale: float,
    seed: int,
    connectors_per_zone: int = 4,
    max_connector_distance_m: float = 5_000.0,
    gateway_connector_path: Path | None = None,
    duarouter_binary: Path | None = None,
    log: Log = print,
) -> dict[str, Any]:
    """Build a checksum-bound, routed, conservation-audited demand package."""

    if sampling_scale < 1:
        raise SumoDemandError("sampling_scale must represent at least one real vehicle")
    if connectors_per_zone < 1:
        raise SumoDemandError("connectors_per_zone must be positive")
    if output_directory.exists():
        raise SumoDemandError("Demand output already exists; choose a new demand version")

    source_report_path = intake_directory / "demand-source-report.json"
    if not source_report_path.is_file():
        source_report_path = intake_directory / "intake-report.json"
    od_path = intake_directory / "od-demand.parquet"
    zones_path = intake_directory / "zones.parquet"
    source_report = _read_json(source_report_path)
    source_kind = str(source_report.get("source_kind", "accepted_regional_od"))
    if source_kind == "local_proxy":
        if source_report.get("simulation_ready") is not True:
            raise SumoDemandError("Local proxy OD source is not simulation_ready")
    elif source_report.get("assignment_ready") is not True:
        raise SumoDemandError("Regional OD intake is not assignment_ready")
    for path in (od_path, zones_path, network_path, network_manifest_path, edge_map_path):
        if not path.is_file():
            raise SumoDemandError(f"Required demand input is missing: {path.name}")

    network_manifest = _read_json(network_manifest_path)
    if _sha256(network_path) != network_manifest["artifact"]["sha256"]:
        raise SumoDemandError("SUMO network checksum does not match its manifest")
    edge_map = pd.read_parquet(edge_map_path)
    if set(edge_map["sumo_network_version"].astype(str).unique()) != {
        str(network_manifest["network_version"])
    }:
        raise SumoDemandError("SUMO edge map does not match the active network version")

    od = pd.read_parquet(od_path)
    zones = gpd.read_parquet(zones_path)

    gateway_connectors: dict[str, dict[str, str]] | None = None

    has_gateway_zones = (
        "zone_type" in zones.columns
        and zones["zone_type"]
        .astype(str)
        .str.lower()
        .eq("gateway")
        .any()
    )

    if has_gateway_zones:
        if gateway_connector_path is None:
            raise SumoDemandError(
                "Gateway OD requires a deterministic "
                "SUMO gateway connector artifact"
            )

        if not gateway_connector_path.is_file():
            raise SumoDemandError(
                "Gateway connector artifact is missing: "
                f"{gateway_connector_path}"
            )

        gateway_connectors = _load_gateway_connector_config(
            gateway_connector_path,
            network_version=str(
                network_manifest["network_version"]
            ),
            network_sha256=str(
                network_manifest["artifact"]["sha256"]
            ),
        )

    selected_od = od.loc[od["period"].astype(str) == period].copy()
    if selected_od.empty:
        raise SumoDemandError(f"Accepted OD demand has no rows for period {period}")
    duration_values = selected_od["period_duration_minutes"].dropna().astype(float).unique()
    if len(duration_values) != 1 or duration_values[0] <= 0:
        raise SumoDemandError("Selected period must have one positive duration")
    duration_seconds = int(round(float(duration_values[0]) * 60))

    log("LOAD checksum-verified SUMO network")
    network = sumolib.net.readNet(str(network_path), withInternal=False)
    required_classes = sorted(
        {_vehicle_mapping(value)[1] for value in selected_od["vehicle_class"]}
    )
    log(f"CONNECT zones={len(zones):,} vehicle_classes={','.join(required_classes)}")
    connectors = build_zone_connectors(
        zones=zones,
        edge_map=edge_map,
        network=network,
        required_sumo_classes=required_classes,
        connectors_per_zone=connectors_per_zone,
        max_distance_m=max_connector_distance_m,
        gateway_connectors=gateway_connectors,
    )
    demanded_zone_ids = set(selected_od["origin_zone_id"].astype(str)) | set(
        selected_od["destination_zone_id"].astype(str)
    )
    connected_zone_ids = set(connectors["zone_id"].astype(str))
    missing_zones = sorted(demanded_zone_ids - connected_zone_ids)
    if missing_zones:
        raise SumoDemandError(
            "Demand zones have no accepted connector: " + ", ".join(missing_zones[:20])
        )

    trips, conservation = sample_od_trips(
        selected_od,
        connectors,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        sampling_scale=sampling_scale,
        seed=seed,
    )
    if not conservation["gate_passed"]:
        raise SumoDemandError("Sampled demand failed the declared conservation gate")
    log(
        f"SAMPLE rows={len(selected_od):,} vehicles={len(trips):,} "
        f"real_trips={conservation['accepted_real_vehicle_trips']:,.3f} scale={sampling_scale:g}"
    )

    output_directory.mkdir(parents=True)
    connector_path = output_directory / "zone-connectors.parquet"
    connectors.to_parquet(connector_path, index=False, compression="zstd")
    connectors.to_csv(output_directory / "zone-connectors-review.csv", index=False)
    vtypes_path = output_directory / "vehicle-types.add.xml"
    _write_vtypes(vtypes_path, sorted(set(trips["sumo_type_id"])))
    trips_path = output_directory / "sampled.trips.xml"
    _write_trips(trips_path, trips)

    route_path = output_directory / "regional.rou.xml"
    alternatives_path = output_directory / "regional.alternatives.xml"
    stdout_path = output_directory / "duarouter.stdout.log"
    stderr_path = output_directory / "duarouter.stderr.log"
    duarouter = _resolve_duarouter(duarouter_binary)
    command = [
        duarouter,
        "--net-file",
        str(network_path),
        "--route-files",
        str(trips_path),
        "--output-file",
        str(route_path),
        "--alternatives-output",
        str(alternatives_path),
        "--seed",
        str(seed),
        "--routing-algorithm",
        "astar",
        "--bulk-routing",
        "true",
        "--routing-threads",
        "4",
        "--ignore-errors",
        "false",
        "--no-step-log",
        "true",
    ]
    log("ROUTE sampled vehicles with duarouter")
    return_code = _run_duarouter(command, stdout_path, stderr_path, log)
    if return_code != 0:
        raise SumoDemandError(
            f"duarouter rejected generated demand; inspect duarouter.stderr.log (exit {return_code})"
        )
    _strip_sumo_generation_comment(route_path)
    route_report = _validate_routed_output(
        route_path,
        expected_count=len(trips),
        connector_edge_ids=set(connectors["sumo_edge_id"].astype(str)),
    )
    compressed_route_path = output_directory / "regional.rou.xml.gz"
    _deterministic_gzip(route_path, compressed_route_path)
    route_path.unlink()
    trips_path.unlink()
    if alternatives_path.exists():
        alternatives_path.unlink()

    connector_records = connectors.drop(columns=["geometry_wkb"], errors="ignore").to_dict(
        orient="records"
    )
    connector_version = hashlib.sha256(
        json.dumps(connector_records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    zone_types = zones.set_index(zones["zone_id"].astype(str))["zone_type"].astype(str)
    origins = selected_od["origin_zone_id"].astype(str).map(zone_types)
    destinations = selected_od["destination_zone_id"].astype(str).map(zone_types)
    movement_summary = {
        "internal_to_internal_real_trips": float(
            selected_od.loc[(origins == "internal") & (destinations == "internal"), "vehicle_trips"].sum()
        ),
        "external_entry_real_trips": float(
            selected_od.loc[(origins.isin({"external", "gateway"})) & (destinations == "internal"), "vehicle_trips"].sum()
        ),
        "external_exit_real_trips": float(
            selected_od.loc[(origins == "internal") & (destinations.isin({"external", "gateway"})), "vehicle_trips"].sum()
        ),
        "external_through_real_trips": float(
            selected_od.loc[
                origins.isin({"external", "gateway"})
                & destinations.isin({"external", "gateway"}),
                "vehicle_trips",
            ].sum()
        ),
    }
    manifest = {
        "schema_version": DEMAND_SCHEMA_VERSION,
        "demand_version": demand_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_level": str(
            source_report.get("evidence_level", "accepted_od_uncalibrated_assignment")
        ),
        "source": {
            "source_kind": source_kind,
            "campaign_id": source_report["campaign_id"],
            "agency": source_report["agency"],
            "model": source_report["model"],
            "assignment_ready": bool(source_report.get("assignment_ready")),
            "simulation_ready": bool(source_report.get("simulation_ready", True)),
            "publication_ready": bool(source_report.get("publication_ready")),
            "license": source_report.get("license", {}),
            "od_sha256": _sha256(od_path),
            "zones_sha256": _sha256(zones_path),
            "source_report_sha256": _sha256(source_report_path),
        },
        "network": {
            "network_version": network_manifest["network_version"],
            "sumo_version": network_manifest["sumo_version"],
            "network_sha256": network_manifest["artifact"]["sha256"],
            "edge_map_sha256": _sha256(edge_map_path),
        },
        "period": {
            "name": period,
            "start_seconds": start_seconds,
            "duration_seconds": duration_seconds,
            "end_seconds": start_seconds + duration_seconds,
        },
        "sampling": {
            "seed": seed,
            "real_vehicles_per_simulated_vehicle": sampling_scale,
            **conservation,
        },
        "connectors": {
            "schema_version": CONNECTOR_SCHEMA_VERSION,
            "connector_version": connector_version,
            "zone_count": int(connectors["zone_id"].nunique()),
            "record_count": len(connectors),
            "gateway_zone_count": int(
                connectors.loc[connectors["gateway"], "zone_id"].nunique()
            ),
            "all_demand_zone_ids_resolved": not missing_zones,
            "max_connector_distance_m": max_connector_distance_m,
            "selection_policy": "preferred_nonlocal_road_class_then_distance_capacity_weight",
            "sha256": _sha256(connector_path),
        },
        "vehicle_classes": {
            str(key): {
                "sumo_type_id": _vehicle_mapping(key)[0],
                "sumo_vclass": _vehicle_mapping(key)[1],
                "simulated_vehicle_count": int(value),
            }
            for key, value in trips["source_vehicle_class"].value_counts().sort_index().items()
        },
        "boundary_behavior": {
            "zone_types": sorted(zones["zone_type"].astype(str).unique().tolist()),
            "external_and_gateway_zones_use_gateway_connectors": True,
            "external_to_external_movements_retained": True,
            **movement_summary,
        },
        "route_validation": route_report,
        "artifacts": {
            "routes": _artifact(compressed_route_path),
            "vehicle_types": _artifact(vtypes_path),
            "zone_connectors": _artifact(connector_path),
        },
        "limitations": [
            (
                "Local proxy OD is fitted to sparse PORTAL constraints and is not an observed regional trip table."
                if source_kind == "local_proxy"
                else "Accepted agency OD is a prior and has not yet been calibrated against held-out counts."
            ),
            "Vehicle type parameters are conservative SUMO defaults, not calibrated Portland behavior.",
            "Sampling scale changes physical density and must pass a later capacity-equivalence gate before production use.",
        ],
    }
    _atomic_text(
        output_directory / "demand-manifest.json", json.dumps(manifest, indent=2) + "\n"
    )
    _atomic_text(output_directory / "validation-report.md", demand_report_markdown(manifest))
    log(
        f"COMPLETE demand={demand_version} routed={route_report['routed_vehicle_count']:,} "
        f"conservation_gate={conservation['gate_passed']}"
    )
    return manifest


def build_zone_connectors(
    *,
    zones: gpd.GeoDataFrame,
    edge_map: pd.DataFrame,
    network: Any,
    required_sumo_classes: list[str],
    connectors_per_zone: int,
    max_distance_m: float,
    gateway_connectors: dict[str, dict[str, str]] | None = None,
) -> pd.DataFrame:
    """Select direction-preserving arterial connectors for every OD zone."""

    required_zone_columns = {"zone_id", "zone_type", "geometry"}
    if not required_zone_columns.issubset(zones.columns):
        raise SumoDemandError("Normalized zones are missing required fields")
    accepted = edge_map.loc[
        (edge_map["status"] == "accepted") & edge_map["sumo_edge_id"].notna()
    ].copy()
    accepted = accepted.drop_duplicates("sumo_edge_id")
    if accepted.empty:
        raise SumoDemandError("SUMO edge map contains no accepted connector candidates")
    accepted["road_class"] = accepted["road_class"].fillna("unknown").astype(str)
    accepted["geometry"] = accepted["sumo_geometry_wkb"].map(_load_wkb)
    accepted = gpd.GeoDataFrame(accepted, geometry="geometry", crs=PROJECTED_CRS)
    edges = {str(edge.getID()): edge for edge in network.getEdges()}
    accepted = accepted.loc[accepted["sumo_edge_id"].astype(str).isin(edges)].copy()
    zones_projected = zones.to_crs(PROJECTED_CRS)
    records: list[dict[str, Any]] = []

    for _, zone in zones_projected.sort_values("zone_id").iterrows():
        zone_id = str(zone["zone_id"])
        zone_type = str(zone["zone_type"]).lower()
        gateway = zone_type in {"external", "gateway"}

        if zone_type == "gateway":
            if gateway_connectors is None:
                raise SumoDemandError(
                    f"Gateway zone {zone_id} has no deterministic "
                    "connector configuration"
                )

            pinned = gateway_connectors.get(zone_id)

            if pinned is None:
                raise SumoDemandError(
                    f"Gateway zone {zone_id} is missing from "
                    "the SUMO gateway connector artifact"
                )

            for connector_type in (
                "origin",
                "destination",
            ):
                sumo_edge_id = pinned[connector_type]
                edge = edges.get(sumo_edge_id)

                if edge is None:
                    raise SumoDemandError(
                        f"Gateway {zone_id} references missing "
                        f"SUMO edge {sumo_edge_id}"
                    )

                usable_classes = [
                    value
                    for value in required_sumo_classes
                    if edge.allows(value)
                ]

                missing_classes = sorted(
                    set(required_sumo_classes)
                    - set(usable_classes)
                )

                if missing_classes:
                    raise SumoDemandError(
                        f"Gateway {zone_id} {connector_type} edge "
                        f"{sumo_edge_id} does not allow: "
                        + ", ".join(missing_classes)
                    )

                if not _has_directional_continuation(
                    edge,
                    connector_type,
                    usable_classes,
                ):
                    raise SumoDemandError(
                        f"Gateway {zone_id} {connector_type} edge "
                        f"{sumo_edge_id} has no legal continuation"
                    )

                records.append(
                    {
                        "connector_schema_version": (
                            CONNECTOR_SCHEMA_VERSION
                        ),
                        "zone_id": zone_id,
                        "zone_type": zone_type,
                        "connector_type": connector_type,
                        "sumo_edge_id": sumo_edge_id,
                        "app_edge_id": (
                            f"gateway:{zone_id}"
                        ),
                        "weight": 1.0,
                        "distance_m": 0.0,
                        "road_class": (
                            "gateway_boundary"
                        ),
                        "gateway": True,
                        "status": "accepted_pinned",
                        "review_reason": pinned[
                            "evidence"
                        ],
                        "allowed_sumo_classes": json.dumps(
                            sorted(usable_classes)
                        ),
                        "geometry_wkb": None,
                    }
                )

            continue

        allowed_classes = (
            EXTERNAL_CONNECTOR_CLASSES
            if gateway
            else INTERNAL_CONNECTOR_CLASSES
        )

        nearby_indices = accepted.sindex.query(
            zone.geometry.buffer(max_distance_m), predicate="intersects"
        )
        candidates = accepted.iloc[nearby_indices]
        candidates = candidates.loc[candidates["road_class"].isin(allowed_classes)].copy()
        candidates["distance_m"] = candidates["geometry"].map(zone.geometry.distance)
        candidates = candidates.loc[candidates["distance_m"] <= max_distance_m].copy()
        if candidates.empty:
            raise SumoDemandError(
                f"Zone {zone_id} has no preferred connector within {max_distance_m:g} meters"
            )
        for connector_type in ("origin", "destination"):
            ranked: list[dict[str, Any]] = []
            for _, candidate in candidates.iterrows():
                sumo_edge_id = str(candidate["sumo_edge_id"])
                edge = edges[sumo_edge_id]
                usable_classes = [
                    value for value in required_sumo_classes if edge.allows(value)
                ]
                if not usable_classes:
                    continue
                if not _has_directional_continuation(edge, connector_type, usable_classes):
                    continue
                distance_m = float(candidate["distance_m"])
                lanes = max(1, len(edge.getLanes()))
                speed = max(1.0, float(edge.getSpeed()))
                hierarchy = CLASS_PRIORITY.get(str(candidate["road_class"]), 0.25)
                raw_weight = hierarchy * lanes * math.sqrt(speed) / (1 + distance_m / 500)
                ranked.append(
                    {
                        "connector_schema_version": CONNECTOR_SCHEMA_VERSION,
                        "zone_id": zone_id,
                        "zone_type": zone_type,
                        "connector_type": connector_type,
                        "sumo_edge_id": sumo_edge_id,
                        "app_edge_id": str(candidate["app_edge_id"]),
                        "weight": raw_weight,
                        "distance_m": distance_m,
                        "road_class": str(candidate["road_class"]),
                        "gateway": gateway,
                        "status": "accepted_auto",
                        "review_reason": (
                            "external_gateway_preferred_class"
                            if gateway
                            else "internal_zone_preferred_class"
                        ),
                        "allowed_sumo_classes": json.dumps(sorted(usable_classes)),
                        "geometry_wkb": bytes(candidate["sumo_geometry_wkb"]),
                    }
                )
            ranked.sort(key=lambda item: (-item["weight"], item["distance_m"], item["sumo_edge_id"]))
            selected = ranked[:connectors_per_zone]
            covered_classes = {
                value
                for item in selected
                for value in json.loads(str(item["allowed_sumo_classes"]))
            }
            for required_class in required_sumo_classes:
                if required_class in covered_classes:
                    continue
                supplement = next(
                    (
                        item
                        for item in ranked
                        if required_class
                        in json.loads(str(item["allowed_sumo_classes"]))
                    ),
                    None,
                )
                if supplement is not None and supplement not in selected:
                    selected.append(supplement)
                    covered_classes.update(
                        json.loads(str(supplement["allowed_sumo_classes"]))
                    )
            if not selected:
                raise SumoDemandError(
                    f"Zone {zone_id} has no {connector_type} connector for required vehicle classes"
                )
            missing_classes = sorted(set(required_sumo_classes) - covered_classes)
            if missing_classes:
                raise SumoDemandError(
                    f"Zone {zone_id} has no {connector_type} connector for: "
                    + ", ".join(missing_classes)
                )
            total_weight = sum(float(item["weight"]) for item in selected)
            for item in selected:
                item["weight"] = float(item["weight"]) / total_weight
                records.append(item)
    return pd.DataFrame.from_records(records).sort_values(
        ["zone_id", "connector_type", "sumo_edge_id"], ignore_index=True
    )


def sample_od_trips(
    od: pd.DataFrame,
    connectors: pd.DataFrame,
    *,
    start_seconds: int,
    duration_seconds: int,
    sampling_scale: float,
    seed: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Deterministically stochastic-round OD values and choose legal connectors."""

    rows = od.sort_values(
        ["origin_zone_id", "destination_zone_id", "vehicle_class"], ignore_index=True
    )
    rng = random.Random(seed)
    exact_counts = rows["vehicle_trips"].astype(float).to_numpy() / sampling_scale
    counts = [math.floor(value) for value in exact_counts]
    exact_total = float(sum(exact_counts))
    target_count = math.floor(exact_total)
    if rng.random() < exact_total - target_count:
        target_count += 1
    remaining = target_count - sum(counts)
    fractional_candidates = [
        (index, float(exact - math.floor(exact)))
        for index, exact in enumerate(exact_counts)
        if exact - math.floor(exact) > 0
    ]
    if remaining > len(fractional_candidates):
        raise SumoDemandError("Balanced sampling could not allocate rounded vehicles")
    ranked_fractional = sorted(
        fractional_candidates,
        key=lambda item: (
            -math.log(max(rng.random(), 1e-15)) / item[1],
            item[0],
        ),
    )
    for index, _fraction in ranked_fractional[:remaining]:
        counts[index] += 1
    records: list[dict[str, Any]] = []
    row_errors: list[float] = []
    per_row: list[dict[str, Any]] = []
    for row_index, row in rows.iterrows():
        source_class = str(row["vehicle_class"]).strip().lower()
        type_id, sumo_vclass = _vehicle_mapping(source_class)
        real_trips = float(row["vehicle_trips"])
        count = counts[row_index]
        represented = count * sampling_scale
        error = represented - real_trips
        row_errors.append(error)
        origins = _eligible_connectors(
            connectors, str(row["origin_zone_id"]), "origin", sumo_vclass
        )
        destinations = _eligible_connectors(
            connectors, str(row["destination_zone_id"]), "destination", sumo_vclass
        )
        if origins.empty or destinations.empty:
            raise SumoDemandError(
                f"No {sumo_vclass} connector for OD "
                f"{row['origin_zone_id']}->{row['destination_zone_id']}"
            )
        for vehicle_index in range(count):
            origin = _weighted_record(origins, rng)
            destination = _weighted_record(destinations, rng)
            if len(destinations) > 1:
                for _ in range(4):
                    if destination["sumo_edge_id"] != origin["sumo_edge_id"]:
                        break
                    destination = _weighted_record(destinations, rng)
            records.append(
                {
                    "id": f"od-{row_index:06d}-{vehicle_index:07d}",
                    "depart": start_seconds + rng.random() * duration_seconds,
                    "from_edge": str(origin["sumo_edge_id"]),
                    "to_edge": str(destination["sumo_edge_id"]),
                    "sumo_type_id": type_id,
                    "sumo_vclass": sumo_vclass,
                    "source_vehicle_class": source_class,
                    "origin_zone_id": str(row["origin_zone_id"]),
                    "destination_zone_id": str(row["destination_zone_id"]),
                }
            )
        per_row.append(
            {
                "origin_zone_id": str(row["origin_zone_id"]),
                "destination_zone_id": str(row["destination_zone_id"]),
                "vehicle_class": source_class,
                "accepted_real_trips": real_trips,
                "simulated_vehicles": count,
                "represented_real_trips": represented,
                "error_real_trips": error,
            }
        )
    trips = pd.DataFrame.from_records(records)
    if trips.empty:
        raise SumoDemandError("Sampling produced zero SUMO vehicles")
    trips = trips.sort_values(["depart", "id"], ignore_index=True)
    accepted_total = float(rows["vehicle_trips"].sum())
    represented_total = float(len(trips) * sampling_scale)
    aggregate_error = represented_total - accepted_total
    per_row_limit = sampling_scale + 1e-9
    aggregate_limit = sampling_scale + 1e-9
    gate = all(abs(value) <= per_row_limit for value in row_errors) and abs(
        aggregate_error
    ) <= aggregate_limit
    return trips, {
        "accepted_od_row_count": len(rows),
        "accepted_real_vehicle_trips": accepted_total,
        "expected_simulated_vehicle_count": accepted_total / sampling_scale,
        "generated_simulated_vehicle_count": len(trips),
        "represented_real_vehicle_trips": represented_total,
        "aggregate_error_real_trips": aggregate_error,
        "maximum_absolute_row_error_real_trips": max(abs(value) for value in row_errors),
        "allowed_absolute_row_error_real_trips": sampling_scale,
        "allowed_absolute_aggregate_error_real_trips": sampling_scale,
        "gate_policy": "balanced_stochastic_rounding_with_row_and_aggregate_error_lte_one_sampling_scale",
        "gate_passed": gate,
        "rows": per_row,
    }


def demand_report_markdown(manifest: dict[str, Any]) -> str:
    sampling = manifest["sampling"]
    connectors = manifest["connectors"]
    routes = manifest["route_validation"]
    return "\n".join(
        [
            "# SUMO regional demand validation",
            "",
            f"- Demand: `{manifest['demand_version']}`",
            f"- Source campaign: `{manifest['source']['campaign_id']}`",
            f"- Period: `{manifest['period']['name']}`",
            f"- Evidence: `{manifest['evidence_level']}`",
            f"- Sampling scale: {sampling['real_vehicles_per_simulated_vehicle']:g} real vehicles per simulated vehicle",
            f"- Accepted real trips: {sampling['accepted_real_vehicle_trips']:,.3f}",
            f"- Generated SUMO vehicles: {sampling['generated_simulated_vehicle_count']:,}",
            f"- Represented real trips: {sampling['represented_real_vehicle_trips']:,.3f}",
            f"- Conservation gate: **{'PASS' if sampling['gate_passed'] else 'FAIL'}**",
            f"- Demand zones resolved: **{'PASS' if connectors['all_demand_zone_ids_resolved'] else 'FAIL'}**",
            f"- Routed vehicles: {routes['routed_vehicle_count']:,}",
            f"- Route gate: **{'PASS' if routes['gate_passed'] else 'FAIL'}**",
            "",
            "This artifact is accepted OD demand routed on the regional network, but it is not yet calibrated against held-out traffic counts.",
            "",
        ]
    )


def _eligible_connectors(
    connectors: pd.DataFrame, zone_id: str, connector_type: str, sumo_vclass: str
) -> pd.DataFrame:
    selected = connectors.loc[
        (connectors["zone_id"].astype(str) == zone_id)
        & (connectors["connector_type"] == connector_type)
    ].copy()
    return selected.loc[
        selected["allowed_sumo_classes"].map(
            lambda value: sumo_vclass in json.loads(str(value))
        )
    ]


def _weighted_record(frame: pd.DataFrame, rng: random.Random) -> dict[str, Any]:
    records = frame.to_dict(orient="records")
    return rng.choices(records, weights=[float(item["weight"]) for item in records], k=1)[0]


def _has_directional_continuation(
    edge: Any, connector_type: str, required_classes: list[str]
) -> bool:
    node = edge.getToNode() if connector_type == "origin" else edge.getFromNode()
    neighbors = node.getOutgoing() if connector_type == "origin" else node.getIncoming()
    return any(
        neighbor.getID() != edge.getID()
        and any(neighbor.allows(value) for value in required_classes)
        for neighbor in neighbors
    )


def _vehicle_mapping(value: Any) -> tuple[str, str]:
    key = str(value).strip().lower()
    try:
        return VEHICLE_CLASS_MAP[key]
    except KeyError as error:
        raise SumoDemandError(
            f"Unsupported vehicle class {key!r}; add an explicit reviewed mapping"
        ) from error


def _write_vtypes(path: Path, type_ids: list[str]) -> None:
    root = ET.Element("additional")
    for type_id in type_ids:
        ET.SubElement(root, "vType", {"id": type_id, **VTYPE_ATTRIBUTES[type_id]})
    _atomic_xml(path, root)


def _write_trips(path: Path, trips: pd.DataFrame) -> None:
    root = ET.Element("routes")
    for type_id in sorted(set(trips["sumo_type_id"])):
        ET.SubElement(root, "vType", {"id": type_id, **VTYPE_ATTRIBUTES[type_id]})
    for trip in trips.to_dict(orient="records"):
        ET.SubElement(
            root,
            "trip",
            {
                "id": str(trip["id"]),
                "type": str(trip["sumo_type_id"]),
                "depart": f"{float(trip['depart']):.3f}",
                "from": str(trip["from_edge"]),
                "to": str(trip["to_edge"]),
                "departLane": "best",
                "departPos": "random_free",
                "arrivalPos": "max",
            },
        )
    _atomic_xml(path, root)


def _validate_routed_output(
    path: Path, *, expected_count: int, connector_edge_ids: set[str]
) -> dict[str, Any]:
    routed = 0
    invalid_endpoints = 0
    empty_routes = 0
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag != "vehicle":
            continue
        routed += 1
        route = element.find("route")
        edges = [] if route is None else str(route.get("edges", "")).split()
        if not edges:
            empty_routes += 1
        elif edges[0] not in connector_edge_ids or edges[-1] not in connector_edge_ids:
            invalid_endpoints += 1
        element.clear()
    gate = routed == expected_count and empty_routes == 0 and invalid_endpoints == 0
    if not gate:
        raise SumoDemandError(
            "Routed demand failed count or connector endpoint validation"
        )
    return {
        "expected_vehicle_count": expected_count,
        "routed_vehicle_count": routed,
        "empty_route_count": empty_routes,
        "invalid_connector_endpoint_count": invalid_endpoints,
        "gate_passed": gate,
    }


def _resolve_duarouter(configured: Path | None) -> str:
    if configured is not None:
        candidate = configured.expanduser().resolve()
        if candidate.is_file():
            return str(candidate)
        raise SumoDemandError(f"Configured duarouter does not exist: {candidate}")
    discovered = shutil.which("duarouter")
    if discovered:
        return discovered
    candidate = Path(sys.executable).parent / "duarouter"
    if candidate.is_file():
        return str(candidate)
    raise SumoDemandError("duarouter is unavailable; run npm run setup")


def _run_duarouter(
    command: list[str], stdout_path: Path, stderr_path: Path, log: Log
) -> int:
    started = monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        try:
            while True:
                try:
                    return process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    log(f"ROUTE still running elapsed={monotonic() - started:0.1f}s")
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise


def _strip_sumo_generation_comment(path: Path) -> None:
    """Remove SUMO's timestamp and absolute command paths from routed output."""

    temporary = path.with_suffix(path.suffix + ".sanitized")
    inside_comment = False
    with path.open("r", encoding="utf-8") as source, temporary.open(
        "w", encoding="utf-8"
    ) as destination:
        for line in source:
            if not inside_comment and "<!-- generated on " in line:
                inside_comment = "-->" not in line
                continue
            if inside_comment:
                if "-->" in line:
                    inside_comment = False
                continue
            destination.write(line)
    temporary.replace(path)


def _load_wkb(value: Any) -> Any:
    if value is None:
        raise SumoDemandError("Accepted connector candidate has no SUMO geometry")
    return wkb.loads(bytes(value))


def _atomic_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    contents = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    _atomic_bytes(path, contents)


def _atomic_text(path: Path, value: str) -> None:
    _atomic_bytes(path, value.encode("utf-8"))


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(value)
        temporary = Path(handle.name)
    temporary.replace(path)


def _deterministic_gzip(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
        with gzip.GzipFile(fileobj=output_handle, mode="wb", filename="", mtime=0) as zipped:
            shutil.copyfileobj(input_handle, zipped)
    temporary.replace(destination)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SumoDemandError(f"Cannot read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise SumoDemandError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
