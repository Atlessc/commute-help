"""Phase 2.2b station projection and native E1 feasibility tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from backend.app.services.sumo.station_cross_section_service import (
    StationCrossSectionError,
    _canonical_portal_station_id,
    _content_digest,
    _promote,
    aggregate_e1_fragments,
    aggregate_lane_detectors,
    project_station_to_relation,
)


def test_raw_and_prefixed_station_identities_join_to_same_canonical_station() -> None:
    assert _canonical_portal_station_id("3175") == "portal-station-3175"
    assert _canonical_portal_station_id("portal-station-3175") == (
        "portal-station-3175"
    )


def test_single_member_projection_preserves_member_and_chain_position() -> None:
    longitude, latitude = _to_wgs84(50, 5)
    result = project_station_to_relation(
        station_id="station-a",
        longitude=longitude,
        latitude=latitude,
        direction="EAST",
        relation=_relation(["a"]),
        member_geometries={"a": _line([(0, 0), (100, 0)])},
        member_lengths_m={"a": 100},
    )
    assert result["sumo_edge_id"] == "a"
    assert result["chain_member_index"] == 0
    assert result["projected_position_m"] == pytest.approx(50, abs=0.02)
    assert result["normalized_position"] == pytest.approx(0.5, abs=0.001)
    assert result["chain_position_m"] == pytest.approx(50, abs=0.02)
    assert result["projection_distance_m"] == pytest.approx(5, abs=0.02)
    assert result["projection_status"] == "review_threshold_policy_unset"


def test_ordered_chain_projection_uses_only_relation_members_and_cumulative_position() -> None:
    longitude, latitude = _to_wgs84(150, 3)
    result = project_station_to_relation(
        station_id="station-b",
        longitude=longitude,
        latitude=latitude,
        direction="EAST",
        relation=_relation(["a", "b"]),
        member_geometries={
            "a": _line([(0, 0), (100, 0)]),
            "b": _line([(100, 0), (200, 0)]),
            "closer-whole-network-edge": _line([(150, 3), (160, 3)]),
        },
        member_lengths_m={"a": 100, "b": 100, "closer-whole-network-edge": 10},
    )
    assert result["sumo_edge_id"] == "b"
    assert result["chain_member_index"] == 1
    assert result["chain_position_m"] == pytest.approx(150, abs=0.02)
    assert result["candidate_member_count"] == 2


def test_member_boundary_and_parallel_overlap_never_choose_file_order() -> None:
    longitude, latitude = _to_wgs84(100, 0)
    relation = _relation(["b", "a"])
    result = project_station_to_relation(
        station_id="boundary",
        longitude=longitude,
        latitude=latitude,
        direction="EAST",
        relation=relation,
        member_geometries={
            "a": _line([(0, 0), (100, 0)]),
            "b": _line([(100, 0), (200, 0)]),
        },
        member_lengths_m={"a": 100, "b": 100},
    )
    assert result["projection_status"] == "review_member_ambiguity"
    assert result["effectively_equal_member_count"] == 2

    overlap = project_station_to_relation(
        station_id="overlap",
        longitude=longitude,
        latitude=latitude,
        direction="EAST",
        relation=_relation(["a", "b"]),
        member_geometries={
            "a": _line([(0, 0), (200, 0)]),
            "b": _line([(0, 0), (200, 0)]),
        },
        member_lengths_m={"a": 200, "b": 200},
    )
    assert overlap["projection_status"] == "review_member_ambiguity"


def test_topology_and_direction_conflicts_remain_review() -> None:
    longitude, latitude = _to_wgs84(50, 0)
    topology = _relation(["a"])
    topology["comparison_eligible"] = False
    result = project_station_to_relation(
        station_id="station",
        longitude=longitude,
        latitude=latitude,
        direction="EAST",
        relation=topology,
        member_geometries={"a": _line([(0, 0), (100, 0)])},
        member_lengths_m={"a": 100},
    )
    assert result["projection_status"] == "review_topology"
    direction = project_station_to_relation(
        station_id="station",
        longitude=longitude,
        latitude=latitude,
        direction="UNKNOWN",
        relation=_relation(["a"]),
        member_geometries={"a": _line([(0, 0), (100, 0)])},
        member_lengths_m={"a": 100},
    )
    assert direction["projection_status"] == "review_direction"


def test_multiple_station_projection_is_independent_and_deterministic() -> None:
    geometry = {"a": _line([(0, 0), (100, 0)])}
    relation = _relation(["a"])
    first_xy = _to_wgs84(25, 2)
    second_xy = _to_wgs84(75, 2)
    first = project_station_to_relation(
        station_id="first", longitude=first_xy[0], latitude=first_xy[1],
        direction="EAST", relation=relation, member_geometries=geometry,
        member_lengths_m={"a": 100},
    )
    second = project_station_to_relation(
        station_id="second", longitude=second_xy[0], latitude=second_xy[1],
        direction="EAST", relation=relation, member_geometries=geometry,
        member_lengths_m={"a": 100},
    )
    assert first["projected_position_m"] == pytest.approx(25, abs=.02)
    assert second["projected_position_m"] == pytest.approx(75, abs=.02)
    repeat = project_station_to_relation(
        station_id="first", longitude=first_xy[0], latitude=first_xy[1],
        direction="EAST", relation=relation, member_geometries=geometry,
        member_lengths_m={"a": 100},
    )
    assert repeat == first


def test_lane_and_fragment_aggregation_is_exact_and_rejects_partial_or_duplicate() -> None:
    lane = aggregate_lane_detectors(
        [
            {"vehicle_count": 4, "mean_speed_mps": 10},
            {"vehicle_count": 6, "mean_speed_mps": 20},
        ],
        900,
    )
    assert lane == {
        "vehicle_count": 10,
        "flow_vph": 40,
        "mean_speed_mps": 16,
        "lane_detector_count": 2,
    }
    fragments = [
        {"begin": 0, "end": 100, "lane_records": [{"detector_id": "a", "vehicle_count": 2, "mean_speed_mps": 10}]},
        {"begin": 100, "end": 200, "lane_records": [{"detector_id": "a", "vehicle_count": 3, "mean_speed_mps": 20}]},
    ]
    assert aggregate_e1_fragments(fragments, interval_start=0, interval_end=200)["vehicle_count"] == 5
    with pytest.raises(StationCrossSectionError, match="incomplete"):
        aggregate_e1_fragments(fragments[:1], interval_start=0, interval_end=200)
    with pytest.raises(StationCrossSectionError, match="gap, overlap"):
        aggregate_e1_fragments([fragments[0], fragments[0]], interval_start=0, interval_end=200)


def test_installed_sumo_e1_mesoscopic_checkpoint_feasibility(tmp_path: Path) -> None:
    binary_dir = Path(sys.executable).parent
    sumo = binary_dir / "sumo"
    netconvert = binary_dir / "netconvert"
    if not sumo.is_file() or not netconvert.is_file():
        pytest.skip("installed SUMO runtime unavailable")
    output = tmp_path / "e1-feasibility.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analyze_station_sumo_cross_sections",
            "--e1-only-output",
            str(output),
            "--e1-work-directory",
            str(tmp_path / "sumo-work"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["feasibility_status"] == "failed_mesoscopic_e1_is_segment_not_lane_cross_section"
    assert report["native_cross_section_supported"] is False
    assert report["simulation_mode"] == "mesoscopic"
    assert report["child_fragment_count"] == 9
    assert report["expected_vehicle_count"] == 20
    assert report["observed_lane_summed_vehicle_count"] == 54
    assert set(report["observed_per_detector_vehicle_count"].values()) == {18}
    assert report["detector_definition_count"] == 3
    assert report["co_located_station_lane_detector_count"] == 2
    assert report["lane_summed_flow_vph"] == 216
    assert report["save_load_completed"] is True
    assert report["checkpoint_fragments_equal_uninterrupted"] is True
    assert report["per_detector_through_vehicle_reconstruction_exact"] is True
    assert report["lane_aggregation_duplicates_segment_traffic"] is True
    assert report["position_separated_detectors_repeat_segment_count"] is True
    assert report["direct_departures_observed_by_mesoscopic_e1"] == 0


def test_deterministic_identity_excludes_generation_time() -> None:
    assert _content_digest({"generated_at": "one", "value": 1}) == _content_digest(
        {"generated_at": "two", "value": 1}
    )


def test_partial_mapping_artifact_cannot_promote(tmp_path: Path) -> None:
    pending = tmp_path / ".pending"
    output = tmp_path / "complete"
    pending.mkdir()
    (pending / "station-sumo-cross-sections.parquet").write_bytes(b"partial")
    with pytest.raises(StationCrossSectionError):
        _promote(pending, output)
    assert not output.exists()


def _relation(edge_ids: list[str]) -> dict[str, object]:
    return {
        "relation_id": "relation",
        "relation_class": "single_edge" if len(edge_ids) == 1 else "ordered_chain_with_side_connections",
        "comparison_eligible": True,
        "ordered_sumo_edge_ids_json": __import__("json").dumps(edge_ids),
        "direction_signature": "u->v",
    }


def _to_wgs84(x: float, y: float) -> tuple[float, float]:
    transformer = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)
    # Offset synthetic coordinates into valid UTM zone-10 coordinates.
    return transformer.transform(500_000 + x, 5_000_000 + y)


def _line(points: list[tuple[float, float]]) -> LineString:
    return LineString([(500_000 + x, 5_000_000 + y) for x, y in points])
