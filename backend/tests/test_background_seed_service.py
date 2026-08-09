from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.services.background_seed_service import (
    _PathRecord,
    assign_edge_flows,
    calibrate_path_weights,
    flow_conservation_error,
)


def _path(
    pair_id: str,
    nodes: tuple[str, ...],
    edges: tuple[str, ...],
    prior: float = 1.0,
) -> _PathRecord:
    return _PathRecord(
        pair_id=pair_id,
        origin_node_id=nodes[0],
        destination_node_id=nodes[-1],
        origin_zone=f"zone-{nodes[0]}",
        destination_zone=f"zone-{nodes[-1]}",
        straight_distance_m=10_000,
        route_free_flow_seconds=600,
        origin_activity=100,
        destination_activity=100,
        prior_weight=prior,
        node_ids=nodes,
        edge_ids=edges,
    )


def test_calibration_fits_training_links_without_using_holdout_target() -> None:
    paths = [
        _path("a", ("1", "2", "3"), ("edge-1", "edge-2")),
        _path("b", ("1", "2", "4"), ("edge-1", "edge-3")),
        _path("c", ("5", "6"), ("edge-4",)),
    ]
    observations = pd.DataFrame.from_records(
        [
            {
                "edge_id": "edge-1",
                "observed_volume_vph": 1000.0,
                "observed_speed_kph": 60.0,
                "validation_group": "corridor-a|N",
                "split": "train",
            },
            {
                "edge_id": "edge-2",
                "observed_volume_vph": 600.0,
                "observed_speed_kph": 55.0,
                "validation_group": "corridor-b|N",
                "split": "train",
            },
            {
                "edge_id": "edge-3",
                "observed_volume_vph": 400.0,
                "observed_speed_kph": 50.0,
                "validation_group": "corridor-c|N",
                "split": "holdout",
            },
        ]
    )

    weights, metrics = calibrate_path_weights(
        paths,
        observations,
        iterations=60,
        damping=0.35,
    )

    assert metrics["train_wape"] < 0.03
    assert metrics["holdout_edge_count"] == 1
    assert metrics["holdout_path_coverage"] == 1.0
    assert abs(weights[1] - 400.0) < 35.0


def test_path_assignment_conserves_flow_at_internal_nodes() -> None:
    paths = [
        _path("a", ("1", "2", "3"), ("edge-1", "edge-2")),
        _path("b", ("3", "2", "4"), ("edge-3", "edge-4")),
    ]
    weights = np.array([125.0, 75.0])

    edge_flows = assign_edge_flows(paths, weights)
    error = flow_conservation_error(paths, weights, edge_flows)

    assert edge_flows == {
        "edge-1": 125.0,
        "edge-2": 125.0,
        "edge-3": 75.0,
        "edge-4": 75.0,
    }
    assert error == 0.0


def test_conservation_check_detects_corrupted_edge_totals() -> None:
    paths = [_path("a", ("1", "2"), ("edge-1",))]
    weights = np.array([100.0])

    assert flow_conservation_error(paths, weights, {"edge-1": 90.0}) == 10.0
