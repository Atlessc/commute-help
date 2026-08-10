"""Gateway-aware candidate paths for the regional proxy OD v2 model."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd

from backend.app.services.background_seed_service import (
    ROAD_CLASS_ROUTE_FACTOR,
    BackgroundSeedConfig,
    _edge_flow_frame,
    _load_nodes,
    _load_priors,
    _observations,
    _routing_graph,
    _shortest_edge_paths,
    assign_edge_flows,
    build_candidate_paths,
    calibrate_path_weights,
    flow_conservation_error,
    select_zone_nodes,
)
from backend.app.services.gateway_inventory_service import (
    load_gateway_inventory,
)

SEED_MODEL_VERSION_V2 = "regional-proxy-od-ipf-v2"

MOVEMENT_INTERNAL_INTERNAL = "internal_internal"
MOVEMENT_GATEWAY_INTERNAL = "gateway_internal"
MOVEMENT_INTERNAL_GATEWAY = "internal_gateway"
MOVEMENT_GATEWAY_GATEWAY = "gateway_gateway"

VALID_MOVEMENT_CLASSES = frozenset(
    {
        MOVEMENT_INTERNAL_INTERNAL,
        MOVEMENT_GATEWAY_INTERNAL,
        MOVEMENT_INTERNAL_GATEWAY,
        MOVEMENT_GATEWAY_GATEWAY,
    }
)

REQUIRED_GATEWAY_COLUMNS = {
    "gateway_id",
    "x",
    "y",
    "activity",
    "inbound_edge_id",
    "inbound_exterior_node_id",
    "inbound_interior_node_id",
    "inbound_cost_seconds",
    "outbound_edge_id",
    "outbound_interior_node_id",
    "outbound_exterior_node_id",
    "outbound_cost_seconds",
}


@dataclass(frozen=True)
class V2PathRecord:
    pair_id: str
    origin_node_id: str
    destination_node_id: str
    origin_zone: str
    destination_zone: str
    origin_zone_type: str
    destination_zone_type: str
    movement_class: str
    straight_distance_m: float
    route_free_flow_seconds: float
    origin_activity: float
    destination_activity: float
    prior_weight: float
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


class BackgroundSeedV2Error(RuntimeError):
    """Gateway-aware background demand cannot be constructed safely."""



def build_background_seed_v2(
    *,
    edge_priors_path: Path,
    nodes_path: Path,
    gateway_inventory_path: Path,
    graph_manifest_path: Path,
    graph_version: str,
    config: BackgroundSeedConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[
    gpd.GeoDataFrame,
    pd.DataFrame,
    dict[str, Any],
]:
    """Build the gateway-aware regional proxy demand seed."""

    settings = config or BackgroundSeedConfig()
    log = progress or (lambda _message: None)

    priors = _load_priors(
        edge_priors_path,
        graph_version,
    )
    nodes = _load_nodes(nodes_path)

    graph, activity = _routing_graph(priors)

    log(
        f"loaded {len(priors):,} edge priors into a "
        f"{graph.number_of_nodes():,}-node routing graph"
    )

    internal_zones = select_zone_nodes(
        nodes,
        activity,
        settings,
    )

    log(
        f"selected {len(internal_zones):,} "
        "internal proxy activity zones"
    )

    gateway_edges, gateway_metadata = (
        load_gateway_inventory(
            gateway_inventory_path,
            graph_manifest_path,
        )
    )

    gateway_zones = prepare_gateway_zones(
        gateway_edges,
        priors,
    )

    log(
        f"loaded {len(gateway_zones):,} "
        "regional gateway zones"
    )

    paths = build_gateway_aware_candidate_paths(
        graph,
        internal_zones,
        gateway_zones,
        settings,
        progress=log,
    )

    if not paths:
        raise BackgroundSeedV2Error(
            "No gateway-aware OD candidate paths were produced."
        )

    od_frame = _v2_path_frame(paths)

    period_results: dict[
        str,
        dict[str, Any],
    ] = {}

    for period, prefix in (
        ("weekday_morning", "am"),
        ("weekday_afternoon", "pm"),
    ):
        observations = _observations(
            priors,
            prefix,
            period,
            settings.holdout_fraction,
        )

        weights, metrics = calibrate_path_weights(
            paths,
            observations,
            iterations=settings.calibration_iterations,
            damping=settings.calibration_damping,
        )

        edge_flows = assign_edge_flows(
            paths,
            weights,
        )

        conservation_error = flow_conservation_error(
            paths,
            weights,
            edge_flows,
        )

        metrics[
            "flow_conservation_max_error_vph"
        ] = conservation_error

        metrics["assigned_od_demand_vph"] = float(
            weights.sum()
        )

        metrics["positive_flow_edge_count"] = len(
            edge_flows
        )

        metrics[
            "positive_flow_edge_percent"
        ] = round(
            100.0
            * len(edge_flows)
            / len(priors),
            4,
        )

        movement_demand: dict[str, float] = {}

        for movement in sorted(
            VALID_MOVEMENT_CLASSES
        ):
            movement_demand[movement] = float(
                sum(
                    weight
                    for path, weight in zip(
                        paths,
                        weights,
                        strict=True,
                    )
                    if path.movement_class
                    == movement
                )
            )

        metrics[
            "movement_demand_vph"
        ] = movement_demand

        metrics["gate_passed"] = bool(
            metrics["holdout_edge_count"]
            >= settings.minimum_pass_holdout_edges
            and metrics["holdout_path_coverage"]
            >= settings.minimum_pass_holdout_coverage
            and metrics["holdout_wape"]
            <= settings.maximum_pass_holdout_wape
            and metrics["positive_flow_edge_percent"]
            >= (
                settings.minimum_pass_positive_flow_coverage
                * 100.0
            )
            and conservation_error <= 1e-6
        )

        period_results[prefix] = {
            "period": period,
            "weights": weights,
            "edge_flows": edge_flows,
            "observations": observations,
            "metrics": metrics,
        }

        od_frame[
            f"{prefix}_demand_vph"
        ] = weights

        log(
            f"{period}: "
            f"train WAPE={metrics['train_wape']:.3f}, "
            f"held-out WAPE="
            f"{metrics['holdout_wape']:.3f}, "
            f"held-out coverage="
            f"{metrics['holdout_path_coverage']:.3f}, "
            f"network flow coverage="
            f"{metrics['positive_flow_edge_percent']:.3f}%, "
            f"gate="
            f"{'pass' if metrics['gate_passed'] else 'diagnostic-only'}"
        )

    edge_flows = _edge_flow_frame(
        priors,
        period_results,
    )

    # _edge_flow_frame is shared with v1 and stamps the
    # v1 model name internally. Correct that provenance here.
    edge_flows[
        "seed_model_version"
    ] = SEED_MODEL_VERSION_V2

    gate_passed = all(
        result["metrics"]["gate_passed"]
        for result in period_results.values()
    )

    gateway_edge_ids = set(
        gateway_edges["edge_id"].astype(str)
    )

    gateway_prior_rows = priors.loc[
        priors["edge_id"]
        .astype(str)
        .isin(gateway_edge_ids)
    ]

    direct_am_gateway_observations = int(
        gateway_prior_rows[
            "historical_am_available"
        ]
        .fillna(False)
        .astype(bool)
        .sum()
    )

    direct_pm_gateway_observations = int(
        gateway_prior_rows[
            "historical_pm_available"
        ]
        .fillna(False)
        .astype(bool)
        .sum()
    )

    movement_counts = movement_class_counts(
        paths
    )

    report = {
        "schema_version": 1,
        "model_version": SEED_MODEL_VERSION_V2,
        "graph_version": graph_version,
        "status": (
            "validation_candidate"
            if gate_passed
            else "diagnostic_only"
        ),
        "evidence_level": "modeled_uncalibrated",
        "boundary_evidence_level": (
            "modeled_unobserved_proxy"
        ),
        "config": asdict(settings),
        "directed_edge_count": len(priors),
        "zone_count": (
            len(internal_zones)
            + len(gateway_zones)
        ),
        "internal_zone_count": len(
            internal_zones
        ),
        "gateway_zone_count": len(
            gateway_zones
        ),
        "od_pair_count": len(paths),
        "movement_class_counts": {
            key: int(value)
            for key, value in sorted(
                movement_counts.items()
            )
        },
        "gateway_inventory": {
            **gateway_metadata,
            "direct_am_observed_edges": (
                direct_am_gateway_observations
            ),
            "direct_pm_observed_edges": (
                direct_pm_gateway_observations
            ),
        },
        "periods": {
            result["period"]: result["metrics"]
            for result in period_results.values()
        },
        "flow_evidence_counts": {
            prefix: {
                str(key): int(value)
                for key, value in edge_flows[
                    f"{prefix}_flow_evidence"
                ]
                .value_counts()
                .items()
            }
            for prefix in ("am", "pm")
        },
        "limitations": [
            (
                "Gateway boundary flows are currently "
                "modeled estimates because no gateway "
                "boundary edge has a direct historical "
                "traffic observation in the active "
                "PORTAL-derived evidence set."
            ),
            (
                "Gateway demand may later be constrained "
                "or replaced by ODOT/WSDOT observations "
                "without changing the downstream SUMO "
                "demand architecture."
            ),
            (
                "Internal OD zones remain network-activity "
                "proxies derived from road capacity and "
                "spatial coverage, not observed household, "
                "employment, or agency trip-table zones."
            ),
            (
                "Only one free-flow path is currently "
                "represented per origin-destination "
                "movement."
            ),
            (
                "Detector constraints cover a limited "
                "freeway/corridor subset of the regional "
                "network."
            ),
            (
                "Zero assigned flow does not establish "
                "zero real-world traffic."
            ),
            (
                "This artifact must remain labeled "
                "modeled_uncalibrated until stronger "
                "boundary and network validation passes."
            ),
        ],
    }

    return edge_flows, od_frame, report


def _v2_path_frame(
    paths: list[V2PathRecord],
) -> pd.DataFrame:
    """Serialize gateway-aware OD paths without losing semantics."""

    return pd.DataFrame.from_records(
        [
            {
                "seed_schema_version": 1,
                "seed_model_version": (
                    SEED_MODEL_VERSION_V2
                ),
                "pair_id": path.pair_id,
                "origin_zone": path.origin_zone,
                "destination_zone": (
                    path.destination_zone
                ),
                "origin_zone_type": (
                    path.origin_zone_type
                ),
                "destination_zone_type": (
                    path.destination_zone_type
                ),
                "movement_class": (
                    path.movement_class
                ),
                "origin_node_id": (
                    path.origin_node_id
                ),
                "destination_node_id": (
                    path.destination_node_id
                ),
                "straight_distance_m": (
                    path.straight_distance_m
                ),
                "route_free_flow_seconds": (
                    path.route_free_flow_seconds
                ),
                "origin_activity": (
                    path.origin_activity
                ),
                "destination_activity": (
                    path.destination_activity
                ),
                "prior_weight": path.prior_weight,
                "route_edge_count": len(
                    path.edge_ids
                ),
                "route_node_ids": json.dumps(
                    path.node_ids,
                    separators=(",", ":"),
                ),
                "route_edge_ids": json.dumps(
                    path.edge_ids,
                    separators=(",", ":"),
                ),
            }
            for path in paths
        ]
    )


def build_gateway_aware_candidate_paths(
    graph: nx.DiGraph,
    internal_zones: pd.DataFrame,
    gateway_zones: pd.DataFrame,
    config: BackgroundSeedConfig,
    progress: Callable[[str], None] | None = None,
) -> list[V2PathRecord]:
    """Build internal and regional-boundary candidate movements."""

    log = progress or (lambda _message: None)

    _validate_internal_zones(internal_zones)
    _validate_gateway_zones(gateway_zones)

    internal_paths = build_candidate_paths(
        graph,
        internal_zones,
        config,
        progress=log,
    )

    paths: list[V2PathRecord] = [
        V2PathRecord(
            pair_id=path.pair_id,
            origin_node_id=path.origin_node_id,
            destination_node_id=path.destination_node_id,
            origin_zone=path.origin_zone,
            destination_zone=path.destination_zone,
            origin_zone_type="internal",
            destination_zone_type="internal",
            movement_class=MOVEMENT_INTERNAL_INTERNAL,
            straight_distance_m=path.straight_distance_m,
            route_free_flow_seconds=path.route_free_flow_seconds,
            origin_activity=path.origin_activity,
            destination_activity=path.destination_activity,
            prior_weight=path.prior_weight,
            node_ids=path.node_ids,
            edge_ids=path.edge_ids,
        )
        for path in internal_paths
    ]

    internal_records = internal_zones.to_dict("records")
    gateway_records = gateway_zones.to_dict("records")

    # Gateway -> internal and gateway -> gateway.
    #
    # Routing begins at the first node *inside* the modeled region.
    # The actual inbound boundary edge is then prepended explicitly.
    for gateway_number, origin in enumerate(
        sorted(
            gateway_records,
            key=lambda row: str(row["gateway_id"]),
        ),
        start=1,
    ):
        source = str(origin["inbound_interior_node_id"])

        internal_targets = {
            str(row["node_id"])
            for row in internal_records
        }

        outbound_gateway_targets = {
            str(row["outbound_interior_node_id"])
            for row in gateway_records
            if str(row["gateway_id"])
            != str(origin["gateway_id"])
        }

        targets = internal_targets | outbound_gateway_targets

        routed = _shortest_edge_paths(
            graph,
            source,
            targets,
        )

        for destination in internal_records:
            target = str(destination["node_id"])
            route = _route_or_zero_length(
                routed,
                source,
                target,
            )

            if route is None:
                continue

            edge_ids, node_ids, travel_seconds = route

            paths.append(
                _make_gateway_internal_path(
                    origin,
                    destination,
                    edge_ids,
                    node_ids,
                    travel_seconds,
                )
            )

        for destination in gateway_records:
            if (
                str(destination["gateway_id"])
                == str(origin["gateway_id"])
            ):
                continue

            target = str(
                destination["outbound_interior_node_id"]
            )
            route = _route_or_zero_length(
                routed,
                source,
                target,
            )

            if route is None:
                continue

            edge_ids, node_ids, travel_seconds = route

            paths.append(
                _make_gateway_gateway_path(
                    origin,
                    destination,
                    edge_ids,
                    node_ids,
                    travel_seconds,
                )
            )

        if (
            gateway_number % 4 == 0
            or gateway_number == len(gateway_records)
        ):
            log(
                "routed "
                f"{gateway_number:,}/{len(gateway_records):,} "
                "gateway origins"
            )

    # Internal -> gateway.
    #
    # Routing stops at the last node inside the modeled region,
    # then the outbound crossing edge is appended explicitly.
    outbound_targets = {
        str(row["outbound_interior_node_id"])
        for row in gateway_records
    }

    for internal_number, origin in enumerate(
        sorted(
            internal_records,
            key=lambda row: str(row["zone_id"]),
        ),
        start=1,
    ):
        source = str(origin["node_id"])

        routed = _shortest_edge_paths(
            graph,
            source,
            outbound_targets,
        )

        for destination in gateway_records:
            target = str(
                destination["outbound_interior_node_id"]
            )
            route = _route_or_zero_length(
                routed,
                source,
                target,
            )

            if route is None:
                continue

            edge_ids, node_ids, travel_seconds = route

            paths.append(
                _make_internal_gateway_path(
                    origin,
                    destination,
                    edge_ids,
                    node_ids,
                    travel_seconds,
                )
            )

        if (
            internal_number % 20 == 0
            or internal_number == len(internal_records)
        ):
            log(
                "routed "
                f"{internal_number:,}/{len(internal_records):,} "
                "internal origins to gateways"
            )

    result = sorted(
        paths,
        key=lambda path: (
            path.movement_class,
            path.pair_id,
        ),
    )

    _validate_candidate_paths(result)

    counts = movement_class_counts(result)

    log(
        "built gateway-aware candidates "
        + ", ".join(
            f"{name}={counts.get(name, 0):,}"
            for name in sorted(VALID_MOVEMENT_CLASSES)
        )
    )

    return result


def _route_or_zero_length(
    routed: dict[
        str,
        tuple[list[str], list[str], float],
    ],
    source: str,
    target: str,
) -> tuple[list[str], list[str], float] | None:
    """Return a routed path or a valid zero-length interior leg."""

    if source == target:
        return [], [source], 0.0

    return routed.get(target)


def movement_class_counts(
    paths: list[V2PathRecord],
) -> dict[str, int]:
    return dict(
        Counter(
            path.movement_class
            for path in paths
        )
    )


def _make_gateway_internal_path(
    origin: dict[str, Any],
    destination: dict[str, Any],
    route_edge_ids: list[str],
    route_node_ids: list[str],
    route_seconds: float,
) -> V2PathRecord:
    origin_zone = str(origin["gateway_id"])
    destination_zone = str(destination["zone_id"])

    nodes = (
        str(origin["inbound_exterior_node_id"]),
        *map(str, route_node_ids),
    )

    edges = (
        str(origin["inbound_edge_id"]),
        *map(str, route_edge_ids),
    )

    return _path(
        origin_zone=origin_zone,
        destination_zone=destination_zone,
        origin_zone_type="gateway",
        destination_zone_type="internal",
        movement_class=MOVEMENT_GATEWAY_INTERNAL,
        origin_node_id=str(
            origin["inbound_exterior_node_id"]
        ),
        destination_node_id=str(
            destination["node_id"]
        ),
        origin_x=float(origin["x"]),
        origin_y=float(origin["y"]),
        destination_x=float(destination["x"]),
        destination_y=float(destination["y"]),
        origin_activity=float(origin["activity"]),
        destination_activity=float(
            destination["activity"]
        ),
        route_seconds=(
            float(origin["inbound_cost_seconds"])
            + float(route_seconds)
        ),
        node_ids=nodes,
        edge_ids=edges,
    )


def _make_internal_gateway_path(
    origin: dict[str, Any],
    destination: dict[str, Any],
    route_edge_ids: list[str],
    route_node_ids: list[str],
    route_seconds: float,
) -> V2PathRecord:
    origin_zone = str(origin["zone_id"])
    destination_zone = str(destination["gateway_id"])

    nodes = (
        *map(str, route_node_ids),
        str(destination["outbound_exterior_node_id"]),
    )

    edges = (
        *map(str, route_edge_ids),
        str(destination["outbound_edge_id"]),
    )

    return _path(
        origin_zone=origin_zone,
        destination_zone=destination_zone,
        origin_zone_type="internal",
        destination_zone_type="gateway",
        movement_class=MOVEMENT_INTERNAL_GATEWAY,
        origin_node_id=str(origin["node_id"]),
        destination_node_id=str(
            destination["outbound_exterior_node_id"]
        ),
        origin_x=float(origin["x"]),
        origin_y=float(origin["y"]),
        destination_x=float(destination["x"]),
        destination_y=float(destination["y"]),
        origin_activity=float(origin["activity"]),
        destination_activity=float(
            destination["activity"]
        ),
        route_seconds=(
            float(route_seconds)
            + float(
                destination["outbound_cost_seconds"]
            )
        ),
        node_ids=nodes,
        edge_ids=edges,
    )


def _make_gateway_gateway_path(
    origin: dict[str, Any],
    destination: dict[str, Any],
    route_edge_ids: list[str],
    route_node_ids: list[str],
    route_seconds: float,
) -> V2PathRecord:
    origin_zone = str(origin["gateway_id"])
    destination_zone = str(destination["gateway_id"])

    nodes = (
        str(origin["inbound_exterior_node_id"]),
        *map(str, route_node_ids),
        str(destination["outbound_exterior_node_id"]),
    )

    edges = (
        str(origin["inbound_edge_id"]),
        *map(str, route_edge_ids),
        str(destination["outbound_edge_id"]),
    )

    return _path(
        origin_zone=origin_zone,
        destination_zone=destination_zone,
        origin_zone_type="gateway",
        destination_zone_type="gateway",
        movement_class=MOVEMENT_GATEWAY_GATEWAY,
        origin_node_id=str(
            origin["inbound_exterior_node_id"]
        ),
        destination_node_id=str(
            destination["outbound_exterior_node_id"]
        ),
        origin_x=float(origin["x"]),
        origin_y=float(origin["y"]),
        destination_x=float(destination["x"]),
        destination_y=float(destination["y"]),
        origin_activity=float(origin["activity"]),
        destination_activity=float(
            destination["activity"]
        ),
        route_seconds=(
            float(origin["inbound_cost_seconds"])
            + float(route_seconds)
            + float(
                destination["outbound_cost_seconds"]
            )
        ),
        node_ids=nodes,
        edge_ids=edges,
    )


def _path(
    *,
    origin_zone: str,
    destination_zone: str,
    origin_zone_type: str,
    destination_zone_type: str,
    movement_class: str,
    origin_node_id: str,
    destination_node_id: str,
    origin_x: float,
    origin_y: float,
    destination_x: float,
    destination_y: float,
    origin_activity: float,
    destination_activity: float,
    route_seconds: float,
    node_ids: tuple[str, ...],
    edge_ids: tuple[str, ...],
) -> V2PathRecord:
    straight_distance_m = float(
        math.hypot(
            destination_x - origin_x,
            destination_y - origin_y,
        )
    )

    distance_km = max(
        straight_distance_m / 1000.0,
        1.0,
    )

    prior_weight = (
        math.sqrt(
            max(origin_activity, 1e-9)
            * max(destination_activity, 1e-9)
        )
        / math.pow(distance_km, 1.15)
    )

    pair_id = hashlib.sha256(
        (
            f"{movement_class}|"
            f"{origin_zone}|"
            f"{destination_zone}"
        ).encode()
    ).hexdigest()[:20]

    return V2PathRecord(
        pair_id=pair_id,
        origin_node_id=origin_node_id,
        destination_node_id=destination_node_id,
        origin_zone=origin_zone,
        destination_zone=destination_zone,
        origin_zone_type=origin_zone_type,
        destination_zone_type=destination_zone_type,
        movement_class=movement_class,
        straight_distance_m=straight_distance_m,
        route_free_flow_seconds=float(route_seconds),
        origin_activity=float(origin_activity),
        destination_activity=float(destination_activity),
        prior_weight=max(float(prior_weight), 1e-9),
        node_ids=node_ids,
        edge_ids=edge_ids,
    )


def _validate_internal_zones(
    zones: pd.DataFrame,
) -> None:
    required = {
        "zone_id",
        "node_id",
        "x",
        "y",
        "activity",
    }

    missing = sorted(
        required - set(zones.columns)
    )

    if missing:
        raise BackgroundSeedV2Error(
            "Internal zones are missing fields: "
            + ", ".join(missing)
        )


def _validate_gateway_zones(
    gateways: pd.DataFrame,
) -> None:
    missing = sorted(
        REQUIRED_GATEWAY_COLUMNS
        - set(gateways.columns)
    )

    if missing:
        raise BackgroundSeedV2Error(
            "Gateway zones are missing fields: "
            + ", ".join(missing)
        )

    if gateways.empty:
        raise BackgroundSeedV2Error(
            "Gateway zones must not be empty."
        )

    if gateways["gateway_id"].duplicated().any():
        raise BackgroundSeedV2Error(
            "Gateway zones must have unique gateway_id values."
        )


def _validate_candidate_paths(
    paths: list[V2PathRecord],
) -> None:
    if not paths:
        raise BackgroundSeedV2Error(
            "No v2 candidate paths were produced."
        )

    pair_ids = [
        path.pair_id
        for path in paths
    ]

    if len(pair_ids) != len(set(pair_ids)):
        raise BackgroundSeedV2Error(
            "V2 candidate pair_id values are not unique."
        )

    for path in paths:
        if (
            path.movement_class
            not in VALID_MOVEMENT_CLASSES
        ):
            raise BackgroundSeedV2Error(
                "Unknown v2 movement class "
                f"{path.movement_class}."
            )

        if not path.edge_ids:
            raise BackgroundSeedV2Error(
                f"V2 path {path.pair_id} has no edges."
            )

        if len(path.node_ids) != len(path.edge_ids) + 1:
            raise BackgroundSeedV2Error(
                f"V2 path {path.pair_id} has inconsistent "
                "node and edge counts."
            )



def prepare_gateway_zones(
    gateway_edges: pd.DataFrame,
    priors: pd.DataFrame,
) -> pd.DataFrame:
    """Collapse directed gateway crossings into one routable zone per corridor."""

    required_gateway = {
        "gateway_id",
        "side",
        "ref",
        "movement",
        "edge_id",
        "interior_node_id",
        "exterior_node_id",
        "crossing_lon",
        "crossing_lat",
    }
    missing_gateway = sorted(
        required_gateway - set(gateway_edges.columns)
    )

    if missing_gateway:
        raise BackgroundSeedV2Error(
            "Gateway inventory is missing fields: "
            + ", ".join(missing_gateway)
        )

    required_priors = {
        "edge_id",
        "u",
        "v",
        "road_class",
        "estimated_capacity_vph",
        "free_flow_seconds",
    }
    missing_priors = sorted(
        required_priors - set(priors.columns)
    )

    if missing_priors:
        raise BackgroundSeedV2Error(
            "Edge priors are missing gateway fields: "
            + ", ".join(missing_priors)
        )

    if priors["edge_id"].astype(str).duplicated().any():
        raise BackgroundSeedV2Error(
            "Edge priors contain duplicate edge_id values."
        )

    prior_lookup = {
        str(row.edge_id): row
        for row in priors.itertuples(index=False)
    }

    records: list[dict[str, Any]] = []

    for gateway_id, group in gateway_edges.groupby(
        "gateway_id",
        sort=True,
    ):
        movements = set(group["movement"].astype(str))

        if len(group) != 2 or movements != {
            "inbound",
            "outbound",
        }:
            raise BackgroundSeedV2Error(
                f"Gateway {gateway_id} must contain exactly "
                "one inbound and one outbound edge."
            )

        inbound = group.loc[
            group["movement"].astype(str) == "inbound"
        ].iloc[0]

        outbound = group.loc[
            group["movement"].astype(str) == "outbound"
        ].iloc[0]

        inbound_prior = prior_lookup.get(
            str(inbound["edge_id"])
        )
        outbound_prior = prior_lookup.get(
            str(outbound["edge_id"])
        )

        if inbound_prior is None:
            raise BackgroundSeedV2Error(
                f"Gateway {gateway_id} inbound edge "
                f"{inbound['edge_id']} is absent from edge priors."
            )

        if outbound_prior is None:
            raise BackgroundSeedV2Error(
                f"Gateway {gateway_id} outbound edge "
                f"{outbound['edge_id']} is absent from edge priors."
            )

        _validate_gateway_edge_direction(
            gateway_id=str(gateway_id),
            movement="inbound",
            inventory_row=inbound,
            prior_row=inbound_prior,
        )

        _validate_gateway_edge_direction(
            gateway_id=str(gateway_id),
            movement="outbound",
            inventory_row=outbound,
            prior_row=outbound_prior,
        )

        inbound_capacity = max(
            float(inbound_prior.estimated_capacity_vph),
            1.0,
        )
        outbound_capacity = max(
            float(outbound_prior.estimated_capacity_vph),
            1.0,
        )

        # Match the v1 internal-zone activity scale:
        # each directed road contributes sqrt(capacity).
        activity = (
            math.sqrt(inbound_capacity)
            + math.sqrt(outbound_capacity)
        )

        records.append(
            {
                "gateway_id": str(gateway_id),
                "side": str(inbound["side"]),
                "ref": str(inbound["ref"]),
                "crossing_lon": (
                    float(inbound["crossing_lon"])
                    + float(outbound["crossing_lon"])
                )
                / 2.0,
                "crossing_lat": (
                    float(inbound["crossing_lat"])
                    + float(outbound["crossing_lat"])
                )
                / 2.0,
                "activity": activity,
                "inbound_edge_id": str(
                    inbound["edge_id"]
                ),
                "inbound_exterior_node_id": str(
                    inbound["exterior_node_id"]
                ),
                "inbound_interior_node_id": str(
                    inbound["interior_node_id"]
                ),
                "inbound_cost_seconds": (
                    _gateway_edge_seed_cost(
                        inbound_prior
                    )
                ),
                "outbound_edge_id": str(
                    outbound["edge_id"]
                ),
                "outbound_interior_node_id": str(
                    outbound["interior_node_id"]
                ),
                "outbound_exterior_node_id": str(
                    outbound["exterior_node_id"]
                ),
                "outbound_cost_seconds": (
                    _gateway_edge_seed_cost(
                        outbound_prior
                    )
                ),
            }
        )

    projected = gpd.GeoDataFrame(
        records,
        geometry=gpd.points_from_xy(
            [row["crossing_lon"] for row in records],
            [row["crossing_lat"] for row in records],
        ),
        crs="EPSG:4326",
    ).to_crs("EPSG:32610")

    projected["x"] = projected.geometry.x
    projected["y"] = projected.geometry.y

    result = pd.DataFrame(
        projected.drop(
            columns=[
                "geometry",
                "crossing_lon",
                "crossing_lat",
            ]
        )
    )

    return result[
        [
            "gateway_id",
            "side",
            "ref",
            "x",
            "y",
            "activity",
            "inbound_edge_id",
            "inbound_exterior_node_id",
            "inbound_interior_node_id",
            "inbound_cost_seconds",
            "outbound_edge_id",
            "outbound_interior_node_id",
            "outbound_exterior_node_id",
            "outbound_cost_seconds",
        ]
    ].sort_values(
        "gateway_id",
        ignore_index=True,
    )


def _gateway_edge_seed_cost(
    prior_row: Any,
) -> float:
    road_class = str(prior_row.road_class)

    return max(
        float(prior_row.free_flow_seconds),
        0.01,
    ) * ROAD_CLASS_ROUTE_FACTOR.get(
        road_class,
        1.2,
    )


def _validate_gateway_edge_direction(
    *,
    gateway_id: str,
    movement: str,
    inventory_row: pd.Series,
    prior_row: Any,
) -> None:
    actual_u = _gateway_node_id(prior_row.u)
    actual_v = _gateway_node_id(prior_row.v)

    exterior = _gateway_node_id(
        inventory_row["exterior_node_id"]
    )
    interior = _gateway_node_id(
        inventory_row["interior_node_id"]
    )

    if movement == "inbound":
        expected = (exterior, interior)
    else:
        expected = (interior, exterior)

    actual = (actual_u, actual_v)

    if actual != expected:
        raise BackgroundSeedV2Error(
            f"Gateway {gateway_id} {movement} edge direction "
            f"does not match the active graph: "
            f"expected {expected[0]}->{expected[1]}, "
            f"found {actual[0]}->{actual[1]}."
        )


def _gateway_node_id(
    value: Any,
) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value)
