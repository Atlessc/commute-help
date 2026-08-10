"""Focused SUMO Phase 5 connector and demand-conservation behavior."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from backend.app.services.sumo.demand_service import (
    SumoDemandError,
    _strip_sumo_generation_comment,
    build_zone_connectors,
    sample_od_trips,
)
from backend.app.services.sumo.proxy_od_service import (
    compile_proxy_od_snapshot,
    proxy_pm_weight,
)
from backend.app.services.traffic_schedule_service import (
    INTERPOLATED_COLUMNS,
    TrafficScheduleService,
)


class _Node:
    def __init__(self) -> None:
        self.incoming: list[_Edge] = []
        self.outgoing: list[_Edge] = []

    def getIncoming(self) -> list[_Edge]:
        return self.incoming

    def getOutgoing(self) -> list[_Edge]:
        return self.outgoing


class _Edge:
    def __init__(self, edge_id: str, start: _Node, end: _Node) -> None:
        self.edge_id = edge_id
        self.start = start
        self.end = end
        start.outgoing.append(self)
        end.incoming.append(self)

    def getID(self) -> str:
        return self.edge_id

    def getFromNode(self) -> _Node:
        return self.start

    def getToNode(self) -> _Node:
        return self.end

    def getLanes(self) -> list[str]:
        return ["lane-1", "lane-2"]

    def getSpeed(self) -> float:
        return 20.0

    def allows(self, value: str) -> bool:
        return value in {"passenger", "truck"}


class _Network:
    def __init__(self, edges: list[_Edge]) -> None:
        self.edges = edges

    def getEdges(self) -> list[_Edge]:
        return self.edges


def _connector_fixture() -> tuple[gpd.GeoDataFrame, pd.DataFrame, _Network]:
    zones = gpd.GeoDataFrame(
        {
            "zone_id": ["Z1", "X1"],
            "zone_type": ["internal", "external"],
        },
        geometry=[
            box(-122.70, 45.50, -122.69, 45.51),
            box(-122.68, 45.50, -122.67, 45.51),
        ],
        crs="EPSG:4326",
    )
    projected = zones.to_crs("EPSG:32610")
    geometries: list[LineString] = []
    records: list[dict[str, object]] = []
    network_edges: list[_Edge] = []
    for index, zone in projected.iterrows():
        center = zone.geometry.centroid
        road_class = "primary" if zone["zone_type"] == "internal" else "motorway"
        first_node, second_node = _Node(), _Node()
        for suffix, offset, start, end in (
            ("a", -4, first_node, second_node),
            ("b", 4, second_node, first_node),
        ):
            edge_id = f"e-{index}-{suffix}"
            geometry = LineString(
                [
                    (center.x - 50, center.y + offset),
                    (center.x + 50, center.y + offset),
                ]
            )
            geometries.append(geometry)
            network_edges.append(_Edge(edge_id, start, end))
            records.append(
                {
                    "status": "accepted",
                    "sumo_edge_id": edge_id,
                    "app_edge_id": f"app-{edge_id}",
                    "road_class": road_class,
                    "sumo_geometry_wkb": bytes(geometry.wkb),
                }
            )
    return zones, pd.DataFrame(records), _Network(network_edges)


def test_zone_connectors_preserve_gateway_and_vehicle_class_evidence() -> None:
    zones, edge_map, network = _connector_fixture()

    connectors = build_zone_connectors(
        zones=zones,
        edge_map=edge_map,
        network=network,
        required_sumo_classes=["passenger", "truck"],
        connectors_per_zone=2,
        max_distance_m=1_000,
    )

    assert set(connectors["zone_id"]) == {"Z1", "X1"}
    assert set(connectors["connector_type"]) == {"origin", "destination"}
    assert connectors.loc[connectors["zone_id"] == "X1", "gateway"].all()
    assert not connectors.loc[connectors["zone_id"] == "Z1", "gateway"].any()
    assert connectors["allowed_sumo_classes"].map(json.loads).map(
        lambda values: set(values) == {"passenger", "truck"}
    ).all()


def test_gateway_zone_uses_pinned_directional_connectors() -> None:
    zones, edge_map, network = _connector_fixture()

    zones.loc[
        zones["zone_id"] == "X1",
        "zone_type",
    ] = "gateway"

    connectors = build_zone_connectors(
        zones=zones,
        edge_map=edge_map,
        network=network,
        required_sumo_classes=[
            "passenger",
            "truck",
        ],
        connectors_per_zone=2,
        max_distance_m=1_000,
        gateway_connectors={
            "X1": {
                "origin": "e-1-a",
                "destination": "e-1-b",
                "evidence": "fixture_exact_gateway",
            }
        },
    )

    gateway = connectors.loc[
        connectors["zone_id"] == "X1"
    ]

    assert len(gateway) == 2

    origin = gateway.loc[
        gateway["connector_type"] == "origin"
    ].iloc[0]

    destination = gateway.loc[
        gateway["connector_type"] == "destination"
    ].iloc[0]

    assert origin["sumo_edge_id"] == "e-1-a"
    assert destination["sumo_edge_id"] == "e-1-b"

    assert (
        gateway["status"]
        == "accepted_pinned"
    ).all()

    assert (
        gateway["weight"] == 1.0
    ).all()


def test_sampling_is_deterministic_and_discloses_bounded_conservation_error() -> None:
    zones, edge_map, network = _connector_fixture()
    connectors = build_zone_connectors(
        zones=zones,
        edge_map=edge_map,
        network=network,
        required_sumo_classes=["passenger", "truck"],
        connectors_per_zone=2,
        max_distance_m=1_000,
    )
    od = pd.DataFrame(
        [
            {
                "origin_zone_id": "Z1",
                "destination_zone_id": "X1",
                "vehicle_class": "SOV",
                "vehicle_trips": 12.0,
            },
            {
                "origin_zone_id": "X1",
                "destination_zone_id": "Z1",
                "vehicle_class": "heavy_truck",
                "vehicle_trips": 7.0,
            },
        ]
    )

    first, first_report = sample_od_trips(
        od,
        connectors,
        start_seconds=25_200,
        duration_seconds=7_200,
        sampling_scale=5,
        seed=42,
    )
    second, second_report = sample_od_trips(
        od,
        connectors,
        start_seconds=25_200,
        duration_seconds=7_200,
        sampling_scale=5,
        seed=42,
    )

    pd.testing.assert_frame_equal(first, second)
    assert first_report == second_report
    assert first_report["gate_passed"] is True
    assert first_report["maximum_absolute_row_error_real_trips"] <= 5
    assert set(first["sumo_type_id"]) == {"passenger_sov", "heavy_truck"}
    assert first["depart"].between(25_200, 32_400, inclusive="left").all()


def test_unknown_vehicle_class_is_blocked() -> None:
    zones, edge_map, network = _connector_fixture()
    connectors = build_zone_connectors(
        zones=zones,
        edge_map=edge_map,
        network=network,
        required_sumo_classes=["passenger"],
        connectors_per_zone=1,
        max_distance_m=1_000,
    )
    od = pd.DataFrame(
        [
            {
                "origin_zone_id": "Z1",
                "destination_zone_id": "X1",
                "vehicle_class": "teleporting_llama",
                "vehicle_trips": 10.0,
            }
        ]
    )
    with pytest.raises(SumoDemandError, match="Unsupported vehicle class"):
        sample_od_trips(
            od,
            connectors,
            start_seconds=0,
            duration_seconds=3_600,
            sampling_scale=1,
            seed=1,
        )


def test_sumo_output_header_does_not_retain_local_paths(tmp_path) -> None:
    route_path = tmp_path / "regional.rou.xml"
    route_path.write_text(
        """<?xml version="1.0"?>
<!-- generated on now by SUMO
<configuration><net-file value="/Users/private/network.xml"/></configuration>
-->
<routes><vehicle id="v1"><route edges="a b"/></vehicle></routes>
""",
        encoding="utf-8",
    )

    _strip_sumo_generation_comment(route_path)

    value = route_path.read_text(encoding="utf-8")
    assert "/Users/private" not in value
    assert "generated on" not in value
    assert "<routes>" in value


def test_proxy_od_snapshot_uses_am_shape_and_schedule_at_morning_anchor(tmp_path) -> None:
    seed_directory = tmp_path / "background"
    seed_directory.mkdir()
    pd.DataFrame(
        [
            {
                "pair_id": "a",
                "origin_zone": "Z1",
                "destination_zone": "Z2",
                "origin_node_id": "1",
                "destination_node_id": "2",
                "am_demand_vph": 100.0,
                "pm_demand_vph": 300.0,
            },
            {
                "pair_id": "b",
                "origin_zone": "Z2",
                "destination_zone": "Z3",
                "origin_node_id": "2",
                "destination_node_id": "3",
                "am_demand_vph": 200.0,
                "pm_demand_vph": 100.0,
            },
        ]
    ).to_parquet(seed_directory / "od-demand-seeds.parquet", index=False)
    (seed_directory / "validation-report.json").write_text(
        json.dumps(
            {
                "graph_version": "fixture-v1",
                "model_version": "proxy-fixture-v1",
                "status": "diagnostic_only",
                "evidence_level": "modeled_uncalibrated",
                "periods": {},
            }
        ),
        encoding="utf-8",
    )
    nodes_path = tmp_path / "nodes.parquet"
    gpd.GeoDataFrame(
        {"osmid": [1, 2, 3]},
        geometry=[
            box(-122.70, 45.50, -122.6999, 45.5001).centroid,
            box(-122.68, 45.50, -122.6799, 45.5001).centroid,
            box(-122.66, 45.50, -122.6599, 45.5001).centroid,
        ],
        crs="EPSG:4326",
    ).to_parquet(nodes_path, index=False)
    graph_manifest_path = tmp_path / "graph-manifest.json"
    graph_manifest_path.write_text(
        json.dumps({"graph_version": "fixture-v1"}), encoding="utf-8"
    )
    schedule_rows = []
    for day_type in ("mon_thu", "friday", "saturday", "sunday"):
        for minute in range(0, 1440, 15):
            row = {
                "profile_version": "schedule-fixture-v1",
                "day_type": day_type,
                "bucket_start_minute": minute,
                "detector_id": "detector-1",
                "app_edge_id": "edge-1",
                "evidence_level": "observed_historical_input",
                "quality_flags": "[]",
            }
            row.update({column: 1_000.0 for column in INTERPOLATED_COLUMNS})
            schedule_rows.append(row)
    schedule_path = tmp_path / "schedule.parquet"
    pd.DataFrame(schedule_rows).to_parquet(schedule_path, index=False)
    manifest_path = tmp_path / "schedule-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schedule_version": "schedule-fixture-v1",
                "artifact": {
                    "sha256": hashlib.sha256(schedule_path.read_bytes()).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    schedule = TrafficScheduleService(schedule_path, manifest_path)
    schedule.load()

    report = compile_proxy_od_snapshot(
        background_seed_directory=seed_directory,
        nodes_path=nodes_path,
        graph_manifest_path=graph_manifest_path,
        schedule_service=schedule,
        departure_time=datetime(
            2026, 8, 3, 7, 30, tzinfo=ZoneInfo("America/Los_Angeles")
        ),
        duration_minutes=60,
        output_directory=tmp_path / "proxy-output",
        source_version="fixture-proxy-v1",
    )

    od = pd.read_parquet(tmp_path / "proxy-output/od-demand.parquet")
    assert proxy_pm_weight(450) == 0
    assert proxy_pm_weight(990) == 1
    assert od["vehicle_trip_rate_vph"].sum() == pytest.approx(300)
    assert od["vehicle_trips"].sum() == pytest.approx(300)
    assert report["simulation_ready"] is True
    assert report["assignment_ready"] is False
    assert report["evidence_level"] == "modeled_uncalibrated"
