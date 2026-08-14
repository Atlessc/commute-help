"""Phase 2.2d projection policy tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.app.services.sumo.station_cross_section_policy_service import (
    StationCrossSectionPolicyError,
    _frame_digest,
    _promote_policy,
    classify_projection,
)


def test_clear_within_edge_acceptance_and_flow_speed_separation() -> None:
    result = classify_projection(
        _row(position=100, upstream=100, downstream=200), ["a"]
    )
    assert result["policy_state"] == "accepted_within_edge"
    assert result["cross_section_type"] == "within_edge_position"
    assert result["accepted_geometry"] is True
    assert result["flow_measurement_eligible"] is True
    assert result["speed_measurement_eligible"] is False


def test_excessive_distance_and_direction_review() -> None:
    far = classify_projection(_row(distance=25.0001), ["a"])
    assert far["policy_state"] == "review_projection_distance"
    assert far["flow_measurement_eligible"] is False
    direction = classify_projection(_row(direction=False), ["a"])
    assert direction["policy_state"] == "review_direction"


def test_internal_sequential_boundary_becomes_transition() -> None:
    downstream = classify_projection(
        _row(index=0, upstream=100, downstream=4), ["a", "b"]
    )
    assert downstream["policy_state"] == "accepted_edge_transition"
    assert downstream["cross_section_type"] == "edge_transition"
    assert downstream["transition_from_sumo_edge_id"] == "a"
    assert downstream["transition_to_sumo_edge_id"] == "b"
    upstream = classify_projection(
        _row(index=1, upstream=0, downstream=100), ["a", "b"]
    )
    assert upstream["policy_state"] == "accepted_edge_transition"


def test_outer_and_non_equivalent_boundaries_remain_review() -> None:
    outer = classify_projection(_row(index=0, upstream=0, downstream=100), ["a", "b"])
    assert outer["policy_state"] == "review_boundary"
    single = classify_projection(_row(upstream=100, downstream=10), ["a"])
    assert single["policy_state"] == "review_boundary"


def test_unmatched_remains_unmatched() -> None:
    result = classify_projection({**_row(), "sumo_edge_id": None}, [])
    assert result["policy_state"] == "unmatched_relation"
    assert result["accepted_geometry"] is False


def test_multiple_station_rows_remain_independent_and_deterministic() -> None:
    import pandas as pd

    frame = pd.DataFrame(
        [
            {"policy_row_id": "one", "station_id": "a", "state": "accepted"},
            {"policy_row_id": "two", "station_id": "b", "state": "accepted"},
        ]
    )
    first = _frame_digest(frame.sort_values("station_id").reset_index(drop=True))
    second = _frame_digest(
        frame.iloc[::-1].sort_values("station_id").reset_index(drop=True)
    )
    assert first == second
    assert frame["policy_row_id"].is_unique


def test_source_hash_is_read_only(tmp_path: Path) -> None:
    source = tmp_path / "source.parquet"
    source.write_bytes(b"immutable-source")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    classify_projection(_row(), ["a"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_partial_policy_output_cannot_promote(tmp_path: Path) -> None:
    pending = tmp_path / ".pending"
    pending.mkdir()
    (pending / "station-cross-section-policy.parquet").write_bytes(b"partial")
    with pytest.raises(StationCrossSectionPolicyError, match="cannot promote"):
        _promote_policy(pending, tmp_path / "complete")
    assert not (tmp_path / "complete").exists()


def _row(
    *,
    distance: float = 1,
    direction: bool = True,
    index: int = 0,
    position: float = 100,
    upstream: float = 100,
    downstream: float = 100,
) -> dict[str, object]:
    return {
        "sumo_edge_id": "a",
        "direction_compatible": direction,
        "projection_distance_m": distance,
        "chain_member_index": index,
        "projected_position_m": position,
        "distance_to_upstream_member_boundary_m": upstream,
        "distance_to_downstream_member_boundary_m": downstream,
    }
