"""Synthetic Phase 2.2a relation topology and aggregation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services.sumo.comparison_relation_service import (
    ComparisonRelationError,
    SumoEdgeEvidence,
    _content_digest,
    _promote,
    _relation_id,
    aggregate_ordered_chain_metrics,
    classify_relation,
)


def test_simple_two_and_three_member_chains_are_ordered_from_topology() -> None:
    two = classify_relation(_rows("a", "b"), _edges(a=("u", "m"), b=("m", "v")))
    assert two["relation_class"] == "ordered_linear_chain"
    assert two["ordered_sumo_edge_ids"] == ["a", "b"]
    assert two["comparison_eligible"] is True
    assert two["primary_flow_comparison_eligible"] is False

    three = classify_relation(
        _rows("c", "a", "b"),
        _edges(a=("u", "m1"), b=("m1", "m2"), c=("m2", "v")),
    )
    assert three["relation_class"] == "ordered_linear_chain"
    assert three["ordered_sumo_edge_ids"] == ["a", "b", "c"]


def test_reversed_input_order_does_not_reverse_the_proven_chain() -> None:
    evidence = _edges(a=("u", "m"), b=("m", "v"))
    forward = classify_relation(_rows("a", "b"), evidence)
    reversed_rows = classify_relation(list(reversed(_rows("a", "b"))), evidence)
    assert reversed_rows["ordered_sumo_edge_ids"] == ["a", "b"]
    assert reversed_rows == forward


def test_disconnected_branch_parallel_and_cycle_are_never_given_a_winner() -> None:
    disconnected = classify_relation(
        _rows("a", "b"), _edges(a=("u", "m"), b=("x", "v"))
    )
    assert disconnected["relation_class"] == "disconnected_candidates"
    assert disconnected["ordered_sumo_edge_ids"] == []

    branch = classify_relation(
        _rows("a", "b", "c"),
        _edges(a=("u", "m"), b=("m", "v"), c=("m", "x")),
    )
    assert branch["relation_class"] == "branching_candidates"
    assert branch["comparison_eligible"] is False

    parallel = classify_relation(
        _rows("a", "b"), _edges(a=("u", "v"), b=("u", "v"))
    )
    assert parallel["relation_class"] == "parallel_candidates"

    cycle = classify_relation(
        _rows("a", "b"), _edges(a=("u", "v"), b=("v", "u"))
    )
    assert cycle["relation_class"] == "cycle_candidates"


def test_direction_conflict_and_duplicate_relation_rows_are_excluded() -> None:
    reversed_direction = _rows("a", "b", app_u="v", app_v="u")
    direction = classify_relation(
        reversed_direction, _edges(a=("u", "m"), b=("m", "v"))
    )
    assert direction["relation_class"] == "direction_conflict"
    assert direction["comparison_eligible"] is False

    rows = _rows("a", "a")
    duplicate = classify_relation(rows, _edges(a=("u", "v")))
    assert duplicate["relation_class"] == "identity_conflict"
    assert duplicate["duplicate_relation_row_count"] == 1


def test_side_connections_preserve_chain_order_but_keep_flow_pending() -> None:
    result = classify_relation(
        _rows("a", "b", "c"),
        _edges(a=("u", "m1"), b=("m1", "m2"), c=("m2", "v")),
        external_incoming={"b": 1},
        external_outgoing={"b": 2},
    )
    assert result["relation_class"] == "ordered_chain_with_side_connections"
    assert result["side_connection_count"] == 3
    assert result["topology_comparison_eligible"] is True
    assert result["primary_flow_comparison_eligible"] is False


def test_chain_metrics_sum_time_use_distance_over_time_and_never_sum_flow() -> None:
    segments = [
        {
            "entered_count": 10.0,
            "departed_count": 2.0,
            "length_m": 1000.0,
            "travel_time_seconds": 100.0,
            "speed_limit_mps": 20.0,
        },
        {
            "entered_count": 1000.0,
            "departed_count": 500.0,
            "length_m": 500.0,
            "travel_time_seconds": 50.0,
            "speed_limit_mps": 10.0,
        },
    ]
    result = aggregate_ordered_chain_metrics(segments)
    assert result.entrance_inflow_count == 12
    assert result.entrance_flow_vph == 48
    assert result.travel_time_seconds == 150
    assert result.traversal_speed_kph == 36
    assert result.reference_travel_time_seconds == 100
    assert result.slowdown == 1.5


def test_missing_segment_traversal_evidence_produces_null_not_fabrication() -> None:
    result = aggregate_ordered_chain_metrics(
        [
            {
                "entered_count": 0.0,
                "departed_count": 0.0,
                "length_m": 100.0,
                "travel_time_seconds": None,
                "speed_limit_mps": 10.0,
            }
        ]
    )
    assert result.entrance_flow_vph == 0
    assert result.travel_time_seconds is None
    assert result.traversal_speed_kph is None
    assert result.slowdown is None


def test_relation_and_content_identity_are_deterministic() -> None:
    assert _relation_id("app", ["a", "b"]) == _relation_id("app", ["a", "b"])
    assert _relation_id("app", ["a", "b"]) != _relation_id("app", ["b", "a"])
    assert _content_digest({"generated_at": "one", "value": 1}) == _content_digest(
        {"generated_at": "two", "value": 1}
    )


def test_partial_output_cannot_promote(tmp_path: Path) -> None:
    pending = tmp_path / ".pending"
    output = tmp_path / "complete"
    pending.mkdir()
    (pending / "app-sumo-comparison-relations.parquet").write_bytes(b"partial")
    with pytest.raises(ComparisonRelationError):
        _promote(pending, output)
    assert not output.exists()


def _rows(
    *edge_ids: str, app_u: str = "u", app_v: str = "v"
) -> list[dict[str, object]]:
    return [
        {
            "app_edge_id": "app",
            "sumo_edge_id": edge_id,
            "graph_version": "graph-v1",
            "sumo_network_version": "sumo-v1",
            "app_u": app_u,
            "app_v": app_v,
            "direction_signature": f"{app_u}->{app_v}",
            "direction_error_degrees": 0.0,
        }
        for edge_id in edge_ids
    ]


def _edges(**values: tuple[str, str]) -> dict[str, SumoEdgeEvidence]:
    return {
        edge_id: SumoEdgeEvidence(
            edge_id=edge_id,
            from_junction=endpoints[0],
            to_junction=endpoints[1],
            length_m=100.0,
            lane_count=2,
            speed_limit_mps=20.0,
            internal=False,
        )
        for edge_id, endpoints in values.items()
    }
