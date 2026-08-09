from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.finalize_portal_campaign import (
    CampaignFinalizationError,
    finalize_campaign,
)


def test_finalize_campaign_writes_enriched_parquet_and_quality_report(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    normalized = campaign / "normalized" / "highway-1_fixture.csv"
    normalized.parent.mkdir(parents=True)
    contents = (
        "station_or_segment_id,timestamp_local,timezone,direction,latitude,longitude,"
        "speed_kph,volume,occupancy,quality_flag,source\n"
        "portal-station-1,2025-09-08T08:00:00-07:00,America/Los_Angeles,NORTH,"
        "45.5,-122.6,80,1200,10,good,PORTAL Highways API campaign\n"
        "portal-station-1,2025-09-08T08:15:00-07:00,America/Los_Angeles,NORTH,"
        "45.5,-122.6,,0,101,low_sample,PORTAL Highways API campaign\n"
    ).encode()
    normalized.write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    chunk_id = "highway-1_2025-09-08_2025-09-08_00-15-00"
    manifest = {
        "schema_version": 2,
        "config": {
            "name": "fixture-v2",
            "start_date": "2025-09-08",
            "end_date": "2025-09-08",
            "resolution": "00:15:00",
            "highway_ids": [1],
            "days_of_week": [2],
            "chunk_days": 1,
        },
        "access_policy": {"chunk_count": 1},
        "chunks": {
            chunk_id: {
                "id": chunk_id,
                "highway_id": 1,
                "start_date": "2025-09-08",
                "end_date": "2025-09-08",
                "status": "complete",
                "rejected_rows": 0,
                "out_of_window_rows": 1,
                "normalized": {
                    "path": "normalized/highway-1_fixture.csv",
                    "bytes": len(contents),
                    "sha256": digest,
                },
            }
        },
    }
    (campaign / "campaign-manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "processed"

    report = finalize_campaign(campaign, output)

    assert report["complete"] is True
    assert report["rows"] == 2
    assert report["profile_eligible_rows"] == 1
    assert report["minute_of_day_buckets"] == [480, 495]
    assert report["source_boundary_rows_removed"] == 1
    parquet_path = next((output / "observations").rglob("*.parquet"))
    frame = pd.read_parquet(parquet_path)
    assert len(frame) == 2
    assert frame["portal_highway_id"].tolist() == [1, 1]
    assert frame["minute_of_day"].tolist() == [480, 495]
    assert frame["profile_eligible"].tolist() == [True, False]
    assert (output / "quality-report.json").exists()
    assert "PORTAL campaign quality report" in (
        output / "quality-report.md"
    ).read_text()
    state = json.loads((output / "finalization-manifest.json").read_text())
    assert state["completed_at"] is not None

    resumed = finalize_campaign(campaign, output)
    assert resumed["newly_processed_chunks"] == 0


def test_finalize_campaign_rejects_midnight_only_schema(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "campaign-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config": {},
                "access_policy": {"chunk_count": 0},
                "chunks": {},
            }
        )
    )

    with pytest.raises(CampaignFinalizationError, match="schema 2"):
        finalize_campaign(campaign, tmp_path / "processed")
