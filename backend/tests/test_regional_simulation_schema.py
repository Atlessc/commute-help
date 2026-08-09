"""Contract tests for generic regional physical-simulation requests."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.app.schemas.simulation import SimulationRunRequest


def test_regional_request_accepts_multiple_generic_restrictions() -> None:
    request = SimulationRunRequest.model_validate(
        {
            "run_kind": "regional_comparison",
            "departure_time": "2026-09-15T07:00:00-07:00",
            "origin_app_edge_id": "origin-edge",
            "destination_app_edge_id": "destination-edge",
            "origin_node_id": "100",
            "destination_node_id": "200",
            "closures": [
                {"app_edge_ids": ["northbound"], "restriction_type": "full"},
                {
                    "app_edge_ids": ["ramp-a", "ramp-b"],
                    "restriction_type": "lane",
                    "remaining_lanes": 1,
                },
                {
                    "app_edge_ids": ["arterial"],
                    "restriction_type": "speed",
                    "speed_limit_kph": 32,
                    "starts_at": "2026-09-15T07:15:00-07:00",
                    "ends_at": "2026-09-15T08:00:00-07:00",
                },
            ],
        }
    )

    assert request.run_kind == "regional_comparison"
    assert request.closures[1].remaining_lanes == 1
    assert request.closures[2].starts_at == datetime.fromisoformat(
        "2026-09-15T07:15:00-07:00"
    )


@pytest.mark.parametrize(
    "closure",
    [
        {"app_edge_ids": ["edge"], "restriction_type": "lane"},
        {"app_edge_ids": ["edge"], "restriction_type": "speed"},
        {
            "app_edge_ids": ["edge"],
            "restriction_type": "full",
            "starts_at": "2026-09-15T07:00:00-07:00",
        },
    ],
)
def test_regional_request_rejects_incomplete_restrictions(closure: dict) -> None:
    with pytest.raises(ValidationError):
        SimulationRunRequest.model_validate(
            {
                "run_kind": "regional_comparison",
                "departure_time": "2026-09-15T07:00:00-07:00",
                "origin_app_edge_id": "origin-edge",
                "destination_app_edge_id": "destination-edge",
                "origin_node_id": "100",
                "destination_node_id": "200",
                "closures": [closure],
            }
        )
