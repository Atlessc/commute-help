"""Focused safety tests for app-edge to SUMO-edge acceptance."""

from backend.app.services.sumo.edge_mapping_service import _mapping_status
from backend.app.services.sumo.network_service import (
    SumoNetworkService,
    UnsafeClosureMappingError,
)

import pandas as pd
import pytest


def test_geometry_chain_requires_high_coverage() -> None:
    assert (
        _mapping_status(
            coverage=0.87,
            max_direction_error=2.0,
            exact_endpoint_candidate=False,
        )
        == "review"
    )


def test_exact_directed_endpoints_allow_junction_geometry_trimming() -> None:
    assert (
        _mapping_status(
            coverage=0.79,
            max_direction_error=2.0,
            exact_endpoint_candidate=True,
        )
        == "accepted"
    )


def test_endpoint_identity_does_not_override_low_coverage_or_wrong_direction() -> None:
    assert (
        _mapping_status(
            coverage=0.69,
            max_direction_error=2.0,
            exact_endpoint_candidate=True,
        )
        == "review"
    )


def test_closure_translation_requires_every_edge_to_be_accepted(tmp_path) -> None:
    service = SumoNetworkService(
        tmp_path / "network.xml", tmp_path / "manifest.json", tmp_path / "map.parquet"
    )
    service.edge_map = pd.DataFrame(
        [
            {"app_edge_id": "safe", "sumo_edge_id": "sumo-a", "status": "accepted"},
            {"app_edge_id": "blocked", "sumo_edge_id": "sumo-b", "status": "review"},
        ]
    )

    assert service.translate_closure_edges(["safe"]) == {"safe": ["sumo-a"]}
    with pytest.raises(UnsafeClosureMappingError) as error:
        service.translate_closure_edges(["safe", "blocked"])
    assert error.value.edge_ids == ["blocked"]
    assert (
        _mapping_status(
            coverage=0.99,
            max_direction_error=46.0,
            exact_endpoint_candidate=True,
        )
        == "review"
    )
