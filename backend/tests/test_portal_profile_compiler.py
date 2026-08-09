from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.db.database import DatabaseManager
from backend.app.services.portal_profile_compiler import (
    compile_profiles,
    register_profiles,
)
from backend.app.services.traffic_service import TrafficService


def test_compiler_builds_bounded_am_pm_artifact_and_registers_profiles(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observations" / "year=2025" / "month=09"
    observations.mkdir(parents=True)
    pd.DataFrame(
        [
            _observation("portal-station-1", "2025-09-08", 480, 80, 1000),
            _observation("portal-station-1", "2025-09-09", 480, 64, 1200),
            _observation("portal-station-1", "2025-09-08", 900, 50, 1400),
            _observation("portal-station-2", "2025-09-08", 480, 70, 800),
            _observation("portal-station-1", "2025-09-08", 720, 60, 900),
        ]
    ).to_parquet(observations / "fixture.parquet", index=False)

    matches_path = tmp_path / "matches.parquet"
    pd.DataFrame(
        [
            {"station_id": "portal-station-1", "edge_id": "edge-1", "status": "accepted"},
            {"station_id": "portal-station-2", "edge_id": "edge-2", "status": "review"},
        ]
    ).to_parquet(matches_path, index=False)
    edges_path = tmp_path / "edges.parquet"
    pd.DataFrame(
        [
            {"edge_id": "edge-1", "maxspeed_kph": 100.0, "estimated_capacity_vph": 2000.0},
            {"edge_id": "edge-2", "maxspeed_kph": 90.0, "estimated_capacity_vph": 1800.0},
        ]
    ).to_parquet(edges_path, index=False)
    output = tmp_path / "profiles"

    report = compile_profiles(
        observations_directory=tmp_path / "observations",
        matches_path=matches_path,
        edges_path=edges_path,
        output_directory=output,
        graph_version="fixture-graph",
        source_campaign="fixture-campaign",
    )

    artifact = pd.read_parquet(output / "edge-bucket-profiles.parquet")
    assert report["accepted_station_count"] == 1
    assert report["selected_period_rows"] == 4
    assert report["matched_period_rows"] == 3
    assert report["excluded_match_rows"] == 1
    assert len(report["profiles"]) == 2
    assert set(artifact["period"]) == {"weekday_morning", "weekday_afternoon"}
    morning = artifact.loc[artifact["period"] == "weekday_morning"].iloc[0]
    assert morning["speed_kph"] == 72
    assert morning["volume"] == 1100
    assert morning["observation_count"] == 2

    campaign_manifest = tmp_path / "campaign-manifest.json"
    campaign_manifest.write_text(json.dumps({"fixture": True}))
    match_report = tmp_path / "station-match-report.json"
    match_report.write_text(
        json.dumps(
            {
                "review_count": 1,
                "unmatched_count": 0,
                "outside_graph_region_count": 0,
            }
        )
    )
    database_path = tmp_path / "app.db"
    register_profiles(
        report=report,
        database_path=database_path,
        campaign_manifest_path=campaign_manifest,
        match_report_path=match_report,
    )
    # Registration is deterministic and safe to resume.
    register_profiles(
        report=report,
        database_path=database_path,
        campaign_manifest_path=campaign_manifest,
        match_report_path=match_report,
    )
    database = DatabaseManager(database_path)
    with database.connect() as connection:
        profile_count = connection.execute("SELECT COUNT(*) FROM traffic_profiles").fetchone()[0]
        import_count = connection.execute("SELECT COUNT(*) FROM traffic_imports").fetchone()[0]
        morning_id = connection.execute(
            "SELECT id FROM traffic_profiles WHERE period = 'weekday_morning'"
        ).fetchone()[0]
    assert profile_count == 2
    assert import_count == 1

    graph_service = SimpleNamespace(
        require_manifest=lambda: SimpleNamespace(
            graph_version="fixture-graph",
            metrics=SimpleNamespace(directed_edges=100),
        )
    )
    traffic = TrafficService(database, graph_service, tmp_path)
    snapshot = traffic.background_snapshot(
        morning_id,
        datetime(2025, 9, 8, 8, 7, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    assert snapshot.bucket_label == "08:00 Pacific weekday"
    assert snapshot.flow_by_edge == {"edge-1": 1100.0}
    assert snapshot.speed_kph_by_edge == {"edge-1": 72.0}
    assert snapshot.observation_count == 2


def _observation(
    station_id: str,
    local_date: str,
    minute: int,
    speed: float,
    volume: float,
) -> dict[str, object]:
    hour, minute_part = divmod(minute, 60)
    return {
        "station_or_segment_id": station_id,
        "timestamp_local": f"{local_date}T{hour:02d}:{minute_part:02d}:00-07:00",
        "local_date": local_date,
        "local_month": 9,
        "iso_weekday": 1,
        "minute_of_day": minute,
        "speed_kph": speed,
        "volume": volume,
        "profile_eligible": True,
    }
