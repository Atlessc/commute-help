import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.services.worldpack_service import (
    WorldPackError,
    WorldPackService,
    WorldPackTimeError,
)


def _write_fixture(
    tmp_path: Path,
) -> tuple[Path, Path]:
    world = tmp_path / "world"
    world.mkdir()

    manifest = {
        "format": "commute-help-worldpack",
        "version": 0,
        "world_id": "wp0-test",
        "classification": (
            "synthetic test fixture; "
            "not calibrated traffic truth"
        ),
        "network": {
            "road_count": 2,
        },
        "simulation": {
            "snapshot_count": 3,
            "sample_interval_seconds": 5,
        },
    }

    (
        world
        / "manifest.json"
    ).write_text(
        json.dumps(
            manifest
        ),
        encoding="utf-8",
    )

    (
        world
        / "road-id-map.json"
    ).write_text(
        json.dumps(
            {
                "format": (
                    "commute-help-worldpack"
                ),
                "version": 0,
                "direction": (
                    "moss_road_id_to_source_sumo_edge_id"
                ),
                "moss_to_source": {
                    "101": "sumo-a",
                    "102": "sumo-b",
                },
                "stats": {
                    "mapped_source_ids": 2,
                    "total_moss_roads": 2,
                    "unmapped_moss_roads": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    np.savez_compressed(
        world
        / "road-static.npz",
        road_ids=np.asarray(
            [101, 102],
            dtype=np.int64,
        ),
        road_length_m=np.asarray(
            [100, 200],
            dtype=np.float32,
        ),
        free_flow_speed_mps=np.asarray(
            [10, 20],
            dtype=np.float32,
        ),
    )

    np.savez_compressed(
        world
        / "road-state.npz",
        sample_times_s=np.asarray(
            [0, 5, 10],
            dtype=np.float32,
        ),
        road_ids=np.asarray(
            [101, 102],
            dtype=np.int64,
        ),
        mean_speed_mps=np.asarray(
            [
                [np.nan, np.nan],
                [0.0, np.nan],
                [5.0, 10.0],
            ],
            dtype=np.float32,
        ),
        vehicle_count=np.asarray(
            [
                [0, 0],
                [1, 0],
                [1, 2],
            ],
            dtype=np.uint32,
        ),
        waiting_vehicle_count=np.asarray(
            [
                [0, 0],
                [1, 0],
                [0, 0],
            ],
            dtype=np.uint32,
        ),
    )

    edge_map = tmp_path / "edge-map.parquet"

    pd.DataFrame(
        [
            {
                "app_edge_id": "app-a",
                "sumo_edge_id": "sumo-a",
                "status": "accepted",
            },
            {
                "app_edge_id": "app-a",
                "sumo_edge_id": "sumo-b",
                "status": "accepted",
            },
            {
                "app_edge_id": "app-outside",
                "sumo_edge_id": "sumo-outside",
                "status": "accepted",
            },
            {
                "app_edge_id": "app-review",
                "sumo_edge_id": "sumo-a",
                "status": "review",
            },
        ]
    ).to_parquet(
        edge_map,
        index=False,
    )

    return world, edge_map


def _service(
    tmp_path: Path,
) -> WorldPackService:
    world, edge_map = _write_fixture(
        tmp_path
    )

    service = WorldPackService(
        world,
        edge_map,
    )

    service.load()

    return service


def test_loads_exact_one_to_many_mapping(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    assert (
        service.world_id
        == "wp0-test"
    )

    assert (
        service.moss_roads_for_app_edge(
            "app-a"
        )
        == (101, 102)
    )

    assert (
        service.moss_roads_for_app_edge(
            "app-review"
        )
        == ()
    )

    assert (
        service.moss_roads_for_app_edge(
            "unknown"
        )
        == ()
    )


def test_reports_mapping_coverage(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    coverage = (
        service.coverage
    )

    assert (
        coverage.worldpack_road_count
        == 2
    )

    assert (
        coverage.worldpack_sources_reached
        == 2
    )

    assert (
        coverage.worldpack_sources_unreached
        == 0
    )

    assert (
        coverage.worldpack_source_coverage_percent
        == 100.0
    )

    assert (
        coverage.inside_relation_count
        == 2
    )

    assert (
        coverage.app_edges_touching_worldpack
        == 1
    )


def test_lookup_never_uses_future_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    assert (
        service.snapshot_index_for_time(
            7
        )
        == 1
    )

    sample = service.road_sample(
        101,
        7,
    )

    assert (
        sample.snapshot_time_s
        == 5
    )


def test_time_before_first_snapshot_fails(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    with pytest.raises(
        WorldPackTimeError
    ):
        service.snapshot_index_for_time(
            -0.01
        )


def test_time_after_world_uses_last_causal_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    sample = service.road_sample(
        101,
        999,
    )

    assert (
        sample.snapshot_time_s
        == 10
    )

    assert (
        sample.raw_mean_speed_mps
        == pytest.approx(
            5
        )
    )


def test_empty_road_uses_free_flow(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    sample = service.road_sample(
        102,
        7,
    )

    assert (
        sample.vehicle_count
        == 0
    )

    assert (
        sample.raw_mean_speed_mps
        is None
    )

    assert (
        sample.speed_source
        == "free_flow"
    )

    assert (
        sample.effective_speed_mps
        == pytest.approx(
            20
        )
    )


def test_zero_speed_uses_explicit_floor(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    sample = service.road_sample(
        101,
        7,
    )

    assert (
        sample.vehicle_count
        == 1
    )

    assert (
        sample.raw_mean_speed_mps
        == pytest.approx(
            0
        )
    )

    assert (
        sample.speed_source
        == "floor"
    )

    assert (
        sample.effective_speed_mps
        == pytest.approx(
            0.1
        )
    )


def test_one_to_many_edge_uses_harmonic_speed(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    sample = service.app_edge_sample(
        "app-a",
        7,
    )

    assert sample.mapped
    assert (
        sample.snapshot_time_s
        == 5
    )

    expected = (
        300
        / (
            100 / 0.1
            + 200 / 20
        )
    )

    assert (
        sample.equivalent_speed_mps
        == pytest.approx(
            expected
        )
    )


def test_uncovered_app_edge_is_explicit(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    sample = service.app_edge_sample(
        "app-outside",
        5,
    )

    assert not sample.mapped
    assert (
        sample.equivalent_speed_mps
        is None
    )
    assert (
        sample.road_samples
        == ()
    )


def test_rejects_incompatible_worldpack_version(
    tmp_path: Path,
) -> None:
    world, edge_map = _write_fixture(
        tmp_path
    )

    manifest_path = (
        world
        / "manifest.json"
    )

    manifest = json.loads(
        manifest_path.read_text()
    )

    manifest["version"] = 999

    manifest_path.write_text(
        json.dumps(
            manifest
        )
    )

    service = WorldPackService(
        world,
        edge_map,
    )

    with pytest.raises(
        WorldPackError,
        match="version",
    ):
        service.load()


def test_rejects_waiting_count_above_vehicle_count(
    tmp_path: Path,
) -> None:
    world, edge_map = _write_fixture(
        tmp_path
    )

    state_path = (
        world
        / "road-state.npz"
    )

    with np.load(
        state_path,
        allow_pickle=False,
    ) as data:
        times = data[
            "sample_times_s"
        ].copy()
        roads = data[
            "road_ids"
        ].copy()
        speeds = data[
            "mean_speed_mps"
        ].copy()
        vehicles = data[
            "vehicle_count"
        ].copy()
        waiting = data[
            "waiting_vehicle_count"
        ].copy()

    waiting[1, 0] = 2

    np.savez_compressed(
        state_path,
        sample_times_s=times,
        road_ids=roads,
        mean_speed_mps=speeds,
        vehicle_count=vehicles,
        waiting_vehicle_count=waiting,
    )

    service = WorldPackService(
        world,
        edge_map,
    )

    with pytest.raises(
        WorldPackError,
        match=(
            "waiting_vehicle_count"
        ),
    ):
        service.load()


def test_loaded_snapshot_times_are_read_only(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path
    )

    with pytest.raises(
        ValueError
    ):
        service.sample_times_s[
            0
        ] = 999
