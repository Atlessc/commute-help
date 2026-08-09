"""Compile conserved, detector-constrained background-flow seed artifacts.

This is a diagnostic normal-network model. It creates path-based OD demand so
vehicles are conserved, fits only training detector edges, and reports held-out
performance before the artifact can influence closure routing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import heapq
import json
import math
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd


SEED_SCHEMA_VERSION = 1
SEED_MODEL_VERSION = "regional-proxy-od-ipf-v1"
BPR_ALPHA = 0.15
BPR_BETA = 4.0

ROAD_CLASS_ROUTE_FACTOR = {
    "motorway": 1.0,
    "motorway_link": 1.03,
    "trunk": 1.0,
    "trunk_link": 1.03,
    "primary": 1.02,
    "primary_link": 1.04,
    "secondary": 1.04,
    "secondary_link": 1.06,
    "tertiary": 1.07,
    "tertiary_link": 1.09,
    "unclassified": 1.13,
    "residential": 1.18,
    "living_street": 1.35,
    "service": 2.5,
    "track": 4.0,
}


class BackgroundSeedError(RuntimeError):
    """The local inputs cannot produce a reproducible demand seed."""


@dataclass(frozen=True)
class BackgroundSeedConfig:
    zone_size_m: int = 5000
    zone_count: int = 80
    minimum_trip_distance_m: int = 3000
    maximum_trip_distance_m: int = 55000
    maximum_od_pairs: int = 6000
    calibration_iterations: int = 40
    calibration_damping: float = 0.35
    holdout_fraction: float = 0.2
    minimum_pass_holdout_edges: int = 20
    minimum_pass_holdout_coverage: float = 0.7
    maximum_pass_holdout_wape: float = 0.5
    minimum_pass_positive_flow_coverage: float = 0.15


@dataclass(frozen=True)
class _PathRecord:
    pair_id: str
    origin_node_id: str
    destination_node_id: str
    origin_zone: str
    destination_zone: str
    straight_distance_m: float
    route_free_flow_seconds: float
    origin_activity: float
    destination_activity: float
    prior_weight: float
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


def build_background_seed(
    *,
    edge_priors_path: Path,
    nodes_path: Path,
    graph_version: str,
    config: BackgroundSeedConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, dict[str, Any]]:
    """Build all-edge flows, OD path weights, and validation evidence."""

    settings = config or BackgroundSeedConfig()
    log = progress or (lambda _message: None)
    priors = _load_priors(edge_priors_path, graph_version)
    nodes = _load_nodes(nodes_path)
    graph, activity = _routing_graph(priors)
    log(f"loaded {len(priors):,} edge priors into a {graph.number_of_nodes():,}-node routing graph")

    zones = select_zone_nodes(nodes, activity, settings)
    log(f"selected {len(zones):,} spatially distributed proxy activity zones")
    paths = build_candidate_paths(graph, zones, settings, log)
    if not paths:
        raise BackgroundSeedError("No routable OD candidate paths were produced.")
    log(f"built {len(paths):,} deterministic OD candidate paths")

    period_results: dict[str, dict[str, Any]] = {}
    od_frame = _path_frame(paths)
    for period, prefix in (("weekday_morning", "am"), ("weekday_afternoon", "pm")):
        observations = _observations(priors, prefix, period, settings.holdout_fraction)
        weights, metrics = calibrate_path_weights(
            paths,
            observations,
            iterations=settings.calibration_iterations,
            damping=settings.calibration_damping,
        )
        edge_flows = assign_edge_flows(paths, weights)
        conservation_error = flow_conservation_error(paths, weights, edge_flows)
        metrics["flow_conservation_max_error_vph"] = conservation_error
        metrics["assigned_od_demand_vph"] = float(weights.sum())
        metrics["positive_flow_edge_count"] = len(edge_flows)
        metrics["positive_flow_edge_percent"] = round(
            100.0 * len(edge_flows) / len(priors), 4
        )
        metrics["gate_passed"] = bool(
            metrics["holdout_edge_count"] >= settings.minimum_pass_holdout_edges
            and metrics["holdout_path_coverage"] >= settings.minimum_pass_holdout_coverage
            and metrics["holdout_wape"] <= settings.maximum_pass_holdout_wape
            and metrics["positive_flow_edge_percent"]
            >= settings.minimum_pass_positive_flow_coverage * 100.0
            and conservation_error <= 1e-6
        )
        period_results[prefix] = {
            "period": period,
            "weights": weights,
            "edge_flows": edge_flows,
            "observations": observations,
            "metrics": metrics,
        }
        od_frame[f"{prefix}_demand_vph"] = weights
        log(
            f"{period}: train WAPE={metrics['train_wape']:.3f}, "
            f"held-out WAPE={metrics['holdout_wape']:.3f}, "
            f"held-out coverage={metrics['holdout_path_coverage']:.3f}, "
            f"network flow coverage={metrics['positive_flow_edge_percent']:.3f}%, "
            f"gate={'pass' if metrics['gate_passed'] else 'diagnostic-only'}"
        )

    edge_flows = _edge_flow_frame(priors, period_results)
    gate_passed = all(result["metrics"]["gate_passed"] for result in period_results.values())
    report = {
        "schema_version": SEED_SCHEMA_VERSION,
        "model_version": SEED_MODEL_VERSION,
        "graph_version": graph_version,
        "status": "validation_candidate" if gate_passed else "diagnostic_only",
        "evidence_level": "modeled_uncalibrated",
        "config": asdict(settings),
        "directed_edge_count": len(priors),
        "zone_count": len(zones),
        "od_pair_count": len(paths),
        "periods": {
            result["period"]: result["metrics"] for result in period_results.values()
        },
        "flow_evidence_counts": {
            prefix: {
                str(key): int(value)
                for key, value in edge_flows[f"{prefix}_flow_evidence"].value_counts().items()
            }
            for prefix in ("am", "pm")
        },
        "limitations": [
            "OD zones are network-activity proxies derived from road capacity and spatial coverage; they are not population, employment, or trip-table observations.",
            "Only one free-flow path is assigned per proxy OD pair in this seed compiler.",
            "Detector constraints cover a small freeway/corridor subset of the regional graph.",
            "Held-out corridor-direction groups are never used during fitting, but nearby graph topology can still provide structural information.",
            "Signal timing, queue spillback, trip purpose, freight share, transit, and time-varying departures are not modeled here.",
            "Zero assigned flow means the proxy OD path set did not use an edge; it does not prove the road has zero real traffic.",
            "This artifact must remain modeled_uncalibrated until a real regional OD matrix and stronger held-out validation replace the proxy demand.",
        ],
    }
    return edge_flows, od_frame, report


def _load_priors(path: Path, graph_version: str) -> gpd.GeoDataFrame:
    if not path.exists():
        raise BackgroundSeedError(f"Edge-prior artifact not found: {path}")
    priors = gpd.read_parquet(path)
    required = {
        "edge_id",
        "u",
        "v",
        "key",
        "road_name",
        "road_class",
        "ref",
        "length_m",
        "resolved_speed_kph",
        "resolved_lanes",
        "estimated_capacity_vph",
        "free_flow_seconds",
        "historical_am_available",
        "historical_am_volume_vph",
        "historical_am_speed_kph",
        "historical_pm_available",
        "historical_pm_volume_vph",
        "historical_pm_speed_kph",
        "geometry",
        "graph_version",
    }
    missing = sorted(required - set(priors.columns))
    if missing:
        raise BackgroundSeedError("Edge priors missing fields: " + ", ".join(missing))
    versions = set(priors["graph_version"].astype(str).unique())
    if versions != {graph_version}:
        raise BackgroundSeedError(
            f"Edge-prior graph versions {sorted(versions)} do not equal {graph_version}."
        )
    if priors["edge_id"].duplicated().any():
        raise BackgroundSeedError("Edge-prior edge_id values are not unique.")
    return priors.reset_index(drop=True)


def _load_nodes(path: Path) -> gpd.GeoDataFrame:
    if not path.exists():
        raise BackgroundSeedError(f"Graph nodes not found: {path}")
    nodes = gpd.read_parquet(path)
    if nodes.crs is None or not {"osmid", "geometry"}.issubset(nodes.columns):
        raise BackgroundSeedError("Graph nodes must contain osmid, geometry, and a CRS.")
    return nodes.to_crs("EPSG:32610").reset_index(drop=True)


def _routing_graph(
    priors: gpd.GeoDataFrame,
) -> tuple[nx.DiGraph, dict[str, float]]:
    graph = nx.DiGraph()
    activity: dict[str, float] = defaultdict(float)
    for row in priors.itertuples(index=False):
        u = _node_id(row.u)
        v = _node_id(row.v)
        capacity = max(float(row.estimated_capacity_vph), 1.0)
        road_class = str(row.road_class)
        cost = max(float(row.free_flow_seconds), 0.01) * ROAD_CLASS_ROUTE_FACTOR.get(
            road_class, 1.2
        )
        existing = graph.get_edge_data(u, v)
        if existing is None or cost < float(existing["seed_cost"]):
            graph.add_edge(
                u,
                v,
                seed_cost=cost,
                edge_id=str(row.edge_id),
            )
        activity[u] += math.sqrt(capacity)
        activity[v] += math.sqrt(capacity)
    return graph, dict(activity)


def select_zone_nodes(
    nodes: gpd.GeoDataFrame,
    activity: dict[str, float],
    config: BackgroundSeedConfig,
) -> pd.DataFrame:
    """Select active, spatially distributed representative nodes."""

    frame = pd.DataFrame(
        {
            "node_id": nodes["osmid"].map(_node_id),
            "x": nodes.geometry.x,
            "y": nodes.geometry.y,
        }
    )
    frame["activity"] = frame["node_id"].map(activity).fillna(0.0)
    frame = frame.loc[frame["activity"] > 0].copy()
    frame["tile_x"] = (frame["x"] // config.zone_size_m).astype(int)
    frame["tile_y"] = (frame["y"] // config.zone_size_m).astype(int)
    representatives = (
        frame.sort_values(["activity", "node_id"], ascending=[False, True])
        .drop_duplicates(["tile_x", "tile_y"])
        .reset_index(drop=True)
    )
    if representatives.empty:
        raise BackgroundSeedError("No active nodes are available for proxy zones.")
    target = min(config.zone_count, len(representatives))
    activity_scale = np.log1p(representatives["activity"].to_numpy(dtype=float))
    activity_scale /= max(float(activity_scale.max()), 1.0)
    coordinates = representatives[["x", "y"]].to_numpy(dtype=float)
    selected = [int(np.argmax(activity_scale))]
    diagonal = max(
        float(np.hypot(np.ptp(coordinates[:, 0]), np.ptp(coordinates[:, 1]))), 1.0
    )
    while len(selected) < target:
        selected_coordinates = coordinates[selected]
        minimum_distance = np.min(
            np.linalg.norm(
                coordinates[:, None, :] - selected_coordinates[None, :, :], axis=2
            ),
            axis=1,
        )
        score = 0.75 * minimum_distance / diagonal + 0.25 * activity_scale
        score[selected] = -1.0
        selected.append(int(np.argmax(score)))
    result = representatives.iloc[selected].copy().reset_index(drop=True)
    result["zone_id"] = result.apply(
        lambda row: f"grid-{int(row.tile_x)}-{int(row.tile_y)}", axis=1
    )
    return result[["zone_id", "node_id", "x", "y", "activity"]]


def build_candidate_paths(
    graph: nx.DiGraph,
    zones: pd.DataFrame,
    config: BackgroundSeedConfig,
    progress: Callable[[str], None] | None = None,
) -> list[_PathRecord]:
    log = progress or (lambda _message: None)
    zone_records = zones.to_dict("records")
    eligible: list[tuple[str, dict[str, Any], dict[str, Any], float]] = []
    for origin in zone_records:
        for destination in zone_records:
            if origin["node_id"] == destination["node_id"]:
                continue
            distance = float(
                math.hypot(origin["x"] - destination["x"], origin["y"] - destination["y"])
            )
            if not (
                config.minimum_trip_distance_m
                <= distance
                <= config.maximum_trip_distance_m
            ):
                continue
            pair_id = hashlib.sha256(
                f"{origin['zone_id']}|{destination['zone_id']}".encode("utf-8")
            ).hexdigest()[:20]
            eligible.append((pair_id, origin, destination, distance))
    if len(eligible) > config.maximum_od_pairs:
        eligible = sorted(eligible, key=lambda value: value[0])[: config.maximum_od_pairs]
    grouped: dict[str, list[tuple[str, dict[str, Any], dict[str, Any], float]]] = defaultdict(list)
    for record in eligible:
        grouped[str(record[1]["node_id"])].append(record)

    paths: list[_PathRecord] = []
    for completed, (origin_node, records) in enumerate(sorted(grouped.items()), start=1):
        targets = {str(record[2]["node_id"]) for record in records}
        routed = _shortest_edge_paths(graph, origin_node, targets)
        for pair_id, origin, destination, distance in records:
            route = routed.get(str(destination["node_id"]))
            if route is None:
                continue
            edge_ids, node_ids, travel_seconds = route
            distance_km = max(distance / 1000.0, 1.0)
            prior_weight = (
                math.sqrt(float(origin["activity"]) * float(destination["activity"]))
                / math.pow(distance_km, 1.15)
            )
            paths.append(
                _PathRecord(
                    pair_id=pair_id,
                    origin_node_id=str(origin["node_id"]),
                    destination_node_id=str(destination["node_id"]),
                    origin_zone=str(origin["zone_id"]),
                    destination_zone=str(destination["zone_id"]),
                    straight_distance_m=distance,
                    route_free_flow_seconds=travel_seconds,
                    origin_activity=float(origin["activity"]),
                    destination_activity=float(destination["activity"]),
                    prior_weight=max(prior_weight, 1e-9),
                    node_ids=tuple(node_ids),
                    edge_ids=tuple(edge_ids),
                )
            )
        if completed % 10 == 0 or completed == len(grouped):
            log(f"routed {completed:,}/{len(grouped):,} proxy zone origins")
    return sorted(paths, key=lambda path: path.pair_id)


def _shortest_edge_paths(
    graph: nx.DiGraph,
    source: str,
    targets: set[str],
) -> dict[str, tuple[list[str], list[str], float]]:
    if source not in graph:
        return {}
    remaining = set(targets)
    distances = {source: 0.0}
    previous: dict[str, tuple[str, str]] = {}
    queue: list[tuple[float, str]] = [(0.0, source)]
    settled: set[str] = set()
    while queue and remaining:
        distance, node = heapq.heappop(queue)
        if node in settled:
            continue
        settled.add(node)
        remaining.discard(node)
        for neighbor, attributes in graph[node].items():
            candidate = distance + float(attributes["seed_cost"])
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = (node, str(attributes["edge_id"]))
                heapq.heappush(queue, (candidate, neighbor))
    results: dict[str, tuple[list[str], list[str], float]] = {}
    for target in targets - remaining:
        edge_ids: list[str] = []
        node_ids = [target]
        current = target
        while current != source:
            predecessor = previous.get(current)
            if predecessor is None:
                edge_ids = []
                node_ids = []
                break
            current, edge_id = predecessor
            edge_ids.append(edge_id)
            node_ids.append(current)
        if edge_ids:
            results[target] = (
                list(reversed(edge_ids)),
                list(reversed(node_ids)),
                float(distances[target]),
            )
    return results


def _observations(
    priors: gpd.GeoDataFrame,
    prefix: str,
    period: str,
    holdout_fraction: float,
) -> pd.DataFrame:
    available = priors[f"historical_{prefix}_available"].fillna(False).astype(bool)
    frame = priors.loc[
        available,
        [
            "edge_id",
            "road_name",
            "ref",
            "geometry",
            f"historical_{prefix}_volume_vph",
            f"historical_{prefix}_speed_kph",
        ],
    ].copy()
    frame = frame.rename(
        columns={
            f"historical_{prefix}_volume_vph": "observed_volume_vph",
            f"historical_{prefix}_speed_kph": "observed_speed_kph",
        }
    )
    frame["bearing_group"] = frame.geometry.map(_bearing_group)
    frame["corridor"] = frame["ref"].fillna("").astype(str).str.strip()
    empty_ref = frame["corridor"] == ""
    frame.loc[empty_ref, "corridor"] = frame.loc[empty_ref, "road_name"].fillna(
        "unnamed"
    ).astype(str)
    frame["validation_group"] = frame["corridor"] + "|" + frame["bearing_group"]
    groups = sorted(frame["validation_group"].unique())
    holdout_groups = {
        group
        for group in groups
        if _hash_fraction(f"{period}|{group}") < holdout_fraction
    }
    if len(groups) >= 5 and not holdout_groups:
        holdout_groups.add(min(groups, key=lambda group: _hash_fraction(f"{period}|{group}")))
    frame["split"] = frame["validation_group"].map(
        lambda group: "holdout" if group in holdout_groups else "train"
    )
    return frame.drop(columns="geometry").reset_index(drop=True)


def calibrate_path_weights(
    paths: list[_PathRecord],
    observations: pd.DataFrame,
    *,
    iterations: int,
    damping: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    edge_to_paths: dict[str, list[int]] = defaultdict(list)
    for index, path in enumerate(paths):
        for edge_id in set(path.edge_ids):
            edge_to_paths[edge_id].append(index)
    prior = np.array([path.prior_weight for path in paths], dtype=float)
    train = observations.loc[observations["split"] == "train"]
    train_covered = train.loc[train["edge_id"].isin(edge_to_paths)]
    if train_covered.empty:
        weights = prior / max(float(prior.sum()), 1.0)
    else:
        raw_predictions = _predict_observations(prior, train_covered, edge_to_paths)
        positive = raw_predictions > 0
        scale = float(
            np.median(
                train_covered.loc[positive, "observed_volume_vph"].to_numpy(dtype=float)
                / raw_predictions[positive]
            )
        )
        weights = prior * max(scale, 1e-9)
        lower = weights * 0.01
        upper = weights * 100.0
        train_edge_ids = train_covered["edge_id"].astype(str).tolist()
        targets = dict(
            zip(
                train_edge_ids,
                train_covered["observed_volume_vph"].to_numpy(dtype=float),
            )
        )
        path_constraints: dict[int, list[str]] = defaultdict(list)
        for edge_id in train_edge_ids:
            for path_index in edge_to_paths[edge_id]:
                path_constraints[path_index].append(edge_id)
        for _iteration in range(iterations):
            predictions = {
                edge_id: float(weights[indexes].sum())
                for edge_id, indexes in edge_to_paths.items()
                if edge_id in targets
            }
            updates = np.zeros(len(paths), dtype=float)
            for path_index, edge_ids in path_constraints.items():
                log_ratios = [
                    math.log(
                        min(
                            4.0,
                            max(
                                0.25,
                                targets[edge_id] / max(predictions[edge_id], 1e-9),
                            ),
                        )
                    )
                    for edge_id in edge_ids
                ]
                updates[path_index] = float(np.mean(log_ratios))
            weights *= np.exp(damping * updates)
            weights = np.clip(weights, lower, upper)

    train_metrics = _validation_metrics(
        observations.loc[observations["split"] == "train"], weights, edge_to_paths
    )
    holdout_metrics = _validation_metrics(
        observations.loc[observations["split"] == "holdout"], weights, edge_to_paths
    )
    metrics = {
        "observation_edge_count": len(observations),
        "train_edge_count": train_metrics["edge_count"],
        "train_path_coverage": train_metrics["path_coverage"],
        "train_mae_vph": train_metrics["mae_vph"],
        "train_wape": train_metrics["wape"],
        "train_median_ape": train_metrics["median_ape"],
        "holdout_edge_count": holdout_metrics["edge_count"],
        "holdout_path_coverage": holdout_metrics["path_coverage"],
        "holdout_mae_vph": holdout_metrics["mae_vph"],
        "holdout_wape": holdout_metrics["wape"],
        "holdout_median_ape": holdout_metrics["median_ape"],
        "holdout_validation_groups": int(
            observations.loc[observations["split"] == "holdout", "validation_group"].nunique()
        ),
    }
    return weights, metrics


def _validation_metrics(
    observations: pd.DataFrame,
    weights: np.ndarray,
    edge_to_paths: dict[str, list[int]],
) -> dict[str, Any]:
    if observations.empty:
        return {
            "edge_count": 0,
            "path_coverage": 0.0,
            "mae_vph": math.inf,
            "wape": math.inf,
            "median_ape": math.inf,
        }
    predictions = _predict_observations(weights, observations, edge_to_paths)
    actual = observations["observed_volume_vph"].to_numpy(dtype=float)
    absolute_error = np.abs(predictions - actual)
    return {
        "edge_count": len(observations),
        "path_coverage": round(float(np.mean(predictions > 0)), 6),
        "mae_vph": round(float(np.mean(absolute_error)), 6),
        "wape": round(float(absolute_error.sum() / max(actual.sum(), 1e-9)), 6),
        "median_ape": round(
            float(np.median(absolute_error / np.maximum(actual, 1e-9))), 6
        ),
    }


def _predict_observations(
    weights: np.ndarray,
    observations: pd.DataFrame,
    edge_to_paths: dict[str, list[int]],
) -> np.ndarray:
    return np.array(
        [
            float(weights[edge_to_paths[str(edge_id)]].sum())
            if str(edge_id) in edge_to_paths
            else 0.0
            for edge_id in observations["edge_id"]
        ],
        dtype=float,
    )


def assign_edge_flows(paths: list[_PathRecord], weights: np.ndarray) -> dict[str, float]:
    flows: dict[str, float] = defaultdict(float)
    for path, weight in zip(paths, weights, strict=True):
        for edge_id in path.edge_ids:
            flows[edge_id] += float(weight)
    return dict(flows)


def flow_conservation_error(
    paths: list[_PathRecord],
    weights: np.ndarray,
    edge_flows: dict[str, float],
) -> float:
    expected: dict[str, float] = defaultdict(float)
    actual: dict[str, float] = defaultdict(float)
    for path, weight in zip(paths, weights, strict=True):
        expected[path.origin_node_id] += float(weight)
        expected[path.destination_node_id] -= float(weight)
        for u, v in zip(path.node_ids, path.node_ids[1:]):
            actual[u] += float(weight)
            actual[v] -= float(weight)
    expected_edge_total = sum(
        float(weight) * len(path.edge_ids)
        for path, weight in zip(paths, weights, strict=True)
    )
    edge_total_error = abs(sum(edge_flows.values()) - expected_edge_total)
    return max(
        [
            edge_total_error,
            *[abs(actual[node] - expected[node]) for node in set(actual) | set(expected)],
        ]
    )


def _edge_flow_frame(
    priors: gpd.GeoDataFrame,
    period_results: dict[str, dict[str, Any]],
) -> gpd.GeoDataFrame:
    output = priors[
        [
            "graph_version",
            "edge_id",
            "u",
            "v",
            "key",
            "road_name",
            "road_class",
            "ref",
            "length_m",
            "resolved_speed_kph",
            "estimated_capacity_vph",
            "free_flow_seconds",
            "geometry",
        ]
    ].copy()
    output["seed_schema_version"] = SEED_SCHEMA_VERSION
    output["seed_model_version"] = SEED_MODEL_VERSION
    for prefix, result in period_results.items():
        flows = result["edge_flows"]
        observations = result["observations"].set_index("edge_id")
        output[f"{prefix}_seed_flow_vph"] = output["edge_id"].map(flows).fillna(0.0)
        output[f"{prefix}_volume_capacity_ratio"] = (
            output[f"{prefix}_seed_flow_vph"] / output["estimated_capacity_vph"].clip(lower=1.0)
        )
        factor = 1.0 + BPR_ALPHA * np.power(
            output[f"{prefix}_volume_capacity_ratio"], BPR_BETA
        )
        output[f"{prefix}_modeled_speed_kph"] = output["resolved_speed_kph"] / factor
        output[f"{prefix}_observed_volume_vph"] = output["edge_id"].map(
            observations["observed_volume_vph"]
        )
        output[f"{prefix}_observed_speed_kph"] = output["edge_id"].map(
            observations["observed_speed_kph"]
        )
        split = output["edge_id"].map(observations["split"])
        output[f"{prefix}_flow_evidence"] = np.select(
            [
                split == "train",
                split == "holdout",
                output[f"{prefix}_seed_flow_vph"] > 0,
            ],
            ["observed_constraint_train", "observed_validation_holdout", "modeled_proxy_path"],
            default="no_proxy_path_flow",
        )
    ordered = [
        "seed_schema_version",
        "seed_model_version",
        *[column for column in output.columns if column not in {"seed_schema_version", "seed_model_version"}],
    ]
    return gpd.GeoDataFrame(output[ordered], geometry="geometry", crs=priors.crs)


def _path_frame(paths: list[_PathRecord]) -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "seed_schema_version": SEED_SCHEMA_VERSION,
                "seed_model_version": SEED_MODEL_VERSION,
                "pair_id": path.pair_id,
                "origin_zone": path.origin_zone,
                "destination_zone": path.destination_zone,
                "origin_node_id": path.origin_node_id,
                "destination_node_id": path.destination_node_id,
                "straight_distance_m": path.straight_distance_m,
                "route_free_flow_seconds": path.route_free_flow_seconds,
                "origin_activity": path.origin_activity,
                "destination_activity": path.destination_activity,
                "prior_weight": path.prior_weight,
                "route_edge_count": len(path.edge_ids),
                "route_node_ids": json.dumps(path.node_ids, separators=(",", ":")),
                "route_edge_ids": json.dumps(path.edge_ids, separators=(",", ":")),
            }
            for path in paths
        ]
    )


def report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Background demand-seed validation report",
        "",
        f"- Status: `{report['status']}`",
        f"- Evidence level: `{report['evidence_level']}`",
        f"- Graph: `{report['graph_version']}`",
        f"- Proxy zones: {report['zone_count']:,}",
        f"- OD candidate paths: {report['od_pair_count']:,}",
        "",
        "## Held-out validation",
        "",
        "| Period | Train edges | Train WAPE | Held-out edges | Held-out coverage | Held-out WAPE | Flow coverage | Gate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for period, metrics in report["periods"].items():
        lines.append(
            f"| `{period}` | {metrics['train_edge_count']:,} | {metrics['train_wape']:.3f} | "
            f"{metrics['holdout_edge_count']:,} | {metrics['holdout_path_coverage']:.3f} | "
            f"{metrics['holdout_wape']:.3f} | {metrics['positive_flow_edge_percent']:.3f}% | "
            f"{'pass' if metrics['gate_passed'] else 'diagnostic only'} |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def _bearing_group(geometry: Any) -> str:
    coordinates = list(geometry.coords)
    start, end = coordinates[0], coordinates[-1]
    bearing = (math.degrees(math.atan2(end[0] - start[0], end[1] - start[1])) + 360.0) % 360.0
    index = int((bearing + 22.5) // 45.0) % 8
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[index]


def _hash_fraction(value: str) -> float:
    number = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    return number / float(2**64)


def _node_id(value: Any) -> str:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)
