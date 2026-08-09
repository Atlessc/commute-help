from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import LineString

from backend.app.services.portal_station_matcher import (
    PortalStation,
    load_portal_stations,
    match_stations,
    report_markdown,
    summarize_matches,
)


TO_WGS84 = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)


def _station(
    *,
    direction: str = "NORTH",
    expected_road_form: str = "mainline",
    route_tokens: tuple[str, ...] = ("I5",),
) -> PortalStation:
    longitude, latitude = TO_WGS84.transform(525_000, 5_040_000)
    return PortalStation(
        station_id="portal-station-1",
        portal_highway_id=1,
        highway_name="I-5",
        direction=direction,
        milepost=300.0,
        location_text="Fixture @ NB I-5 MP300",
        longitude=longitude,
        latitude=latitude,
        expected_road_form=expected_road_form,
        target_route_tokens=route_tokens,
    )


def _edges(records: list[dict[str, object]]) -> gpd.GeoDataFrame:
    frame = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:32610")
    frame["_route_tokens"] = frame["ref"].map(
        lambda value: tuple([str(value).replace(" ", "")]) if value else tuple()
    )
    frame["_bearing"] = frame["geometry"].map(
        lambda geometry: 0.0 if geometry.coords[-1][1] > geometry.coords[0][1] else 180.0
    )
    _ = frame.sindex
    return frame


def _edge(
    edge_id: str,
    geometry: LineString,
    *,
    road_class: str = "motorway",
    ref: str = "I 5",
) -> dict[str, object]:
    return {
        "u": f"{edge_id}-u",
        "v": f"{edge_id}-v",
        "key": "0",
        "edge_id": edge_id,
        "road_name": "Interstate 5",
        "road_class": road_class,
        "ref": ref,
        "geometry": geometry,
    }


def test_matcher_chooses_the_directed_carriageway() -> None:
    edges = _edges(
        [
            _edge("north", LineString([(525_005, 5_039_900), (525_005, 5_040_100)])),
            _edge("south", LineString([(524_995, 5_040_100), (524_995, 5_039_900)])),
        ]
    )

    result = match_stations([_station()], edges).iloc[0]

    assert result["edge_id"] == "north"
    assert result["status"] == "accepted"
    assert result["confidence"] == "high"
    assert result["bearing_difference_degrees"] == 0


def test_matcher_routes_ambiguous_link_and_mainline_to_review() -> None:
    edges = _edges(
        [
            _edge("mainline", LineString([(525_002, 5_039_900), (525_002, 5_040_100)])),
            _edge(
                "link",
                LineString([(524_998, 5_039_900), (524_998, 5_040_100)]),
                road_class="motorway_link",
                ref="",
            ),
        ]
    )

    result = match_stations(
        [
            _station(
                expected_road_form="unknown",
                route_tokens=tuple(),
            )
        ],
        edges,
    ).iloc[0]

    assert result["status"] == "review"
    assert "ambiguous_competing_edge" in result["review_reasons"]


def test_matcher_does_not_accept_a_conflicting_route_reference() -> None:
    edges = _edges(
        [
            _edge(
                "wrong-road",
                LineString([(525_000, 5_039_900), (525_000, 5_040_100)]),
                ref="I 205",
            )
        ]
    )

    result = match_stations([_station()], edges).iloc[0]

    assert result["status"] == "review"
    assert "route_reference_mismatch" in result["review_reasons"]


def test_load_portal_stations_uses_only_observed_station_grain(tmp_path: Path) -> None:
    stations_path = tmp_path / "stations.json"
    highways_path = tmp_path / "highways.json"
    stations_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-13656400, 5700000]},
                        "properties": {
                            "stationid": 1,
                            "highwayid": 1,
                            "milepost": 300,
                            "locationtext": "Fixture @ NB I-5 MP300",
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-13656500, 5700100]},
                        "properties": {
                            "stationid": 2,
                            "highwayid": 1,
                            "locationtext": "No observations",
                        },
                    },
                ],
            }
        )
    )
    highways_path.write_text(
        json.dumps(
            [
                {
                    "highwayid": 1,
                    "highwayname": "I-5",
                    "direction": "NORTH",
                }
            ]
        )
    )

    stations = load_portal_stations(
        stations_path,
        highways_path,
        {"portal-station-1"},
    )

    assert [station.station_id for station in stations] == ["portal-station-1"]
    assert stations[0].target_route_tokens == ("I5",)
    assert stations[0].expected_road_form == "mainline"


def test_report_exposes_review_rows_and_calibration_gate() -> None:
    edges = _edges(
        [
            _edge(
                "wrong-road",
                LineString([(525_000, 5_039_900), (525_000, 5_040_100)]),
                ref="I 205",
            )
        ]
    )
    matches = match_stations([_station()], edges)
    report = summarize_matches(
        matches,
        graph_version="fixture-graph",
        source_campaign="fixture-campaign",
    )

    markdown = report_markdown(report, matches)

    assert report["station_count"] == 1
    assert report["review_count"] == 1
    assert report["accepted_count"] == 0
    assert "route_reference_mismatch" in markdown
    assert "does not change SQLite" in markdown
