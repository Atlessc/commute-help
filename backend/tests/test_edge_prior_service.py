from __future__ import annotations

import pandas as pd

from backend.app.services.edge_prior_service import (
    _normalize_road_name,
    _node_id,
    _parse_osm_speed_kph,
    _select_authority_matches,
    report_markdown,
)


def test_osm_speed_parser_preserves_units_and_uses_conservative_list_value() -> None:
    assert _parse_osm_speed_kph("35 mph") == 35 * 1.609344
    assert _parse_osm_speed_kph("50") == 50
    assert _parse_osm_speed_kph(["45 mph", "35 mph"]) == 35 * 1.609344
    assert _parse_osm_speed_kph("signals") is None


def test_road_name_normalization_handles_direction_and_common_suffixes() -> None:
    assert _normalize_road_name("NE Martin Luther King Jr Boulevard") == (
        "MARTIN LUTHER KING JR BLVD"
    )
    assert _normalize_road_name("Southwest Ericwood Lane") == "ERICWOOD LN"


def test_node_id_canonicalizes_spatial_join_float_coercion() -> None:
    assert _node_id(123.0) == "123"
    assert _node_id(123) == "123"


def test_competing_authority_speeds_are_withheld_for_review() -> None:
    candidates = pd.DataFrame.from_records(
        [
            {
                "edge_id": "edge-1",
                "authority_source": "authority_pbot",
                "authority_source_id": "1",
                "authority_speed_kph": 40.0,
                "authority_total_thru_lanes": None,
                "authority_adt": None,
                "authority_adt_year": None,
                "authority_match_score": 94.0,
                "authority_distance_m": 2.0,
                "authority_overlap_fraction": 0.95,
                "authority_angle_difference_degrees": 2.0,
                "authority_name_agreement": True,
                "eligible": True,
            },
            {
                "edge_id": "edge-1",
                "authority_source": "authority_odot",
                "authority_source_id": "2",
                "authority_speed_kph": 56.0,
                "authority_total_thru_lanes": None,
                "authority_adt": None,
                "authority_adt_year": None,
                "authority_match_score": 92.0,
                "authority_distance_m": 3.0,
                "authority_overlap_fraction": 0.93,
                "authority_angle_difference_degrees": 3.0,
                "authority_name_agreement": True,
                "eligible": True,
            },
        ]
    )

    selected, review = _select_authority_matches(candidates)

    assert selected.empty
    assert len(review) == 2
    assert set(review["review_reason"]) == {"competing_speed_values"}


def test_authority_match_confidence_is_explicit() -> None:
    candidates = pd.DataFrame.from_records(
        [
            {
                "edge_id": "edge-1",
                "authority_source": "authority_clark",
                "authority_source_id": "42",
                "authority_speed_kph": 40.2336,
                "authority_total_thru_lanes": 2,
                "authority_adt": 1200,
                "authority_adt_year": 2024,
                "authority_match_score": 96.0,
                "authority_distance_m": 3.0,
                "authority_overlap_fraction": 0.9,
                "authority_angle_difference_degrees": 4.0,
                "authority_name_agreement": True,
                "eligible": True,
            }
        ]
    )

    selected, review = _select_authority_matches(candidates)

    assert review.empty
    assert selected.iloc[0]["authority_match_confidence"] == "high"
    assert selected.iloc[0]["authority_total_thru_lanes"] == 2


def test_report_calls_out_unresolved_stop_direction_and_no_fake_volume() -> None:
    report = {
        "graph_version": "fixture",
        "directed_edges": 3,
        "authority_review_rows": 1,
        "historically_observed_edge_percent": 0.0,
        "unresolved_stop_direction_edges": 2,
        "speed_source_counts": {"road_class_default": 3},
        "lane_source_counts": {"road_class_default": 3},
        "control_type_counts": {"missing": 3},
        "traffic_evidence_counts": {"unobserved": 3},
        "limitations": [
            "Stop direction is unresolved.",
            "Unobserved edges do not receive synthetic background volume.",
        ],
    }

    markdown = report_markdown(report)

    assert "unresolved approach direction: 2" in markdown
    assert "do not receive synthetic background volume" in markdown
