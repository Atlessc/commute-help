from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from backend.app.services.regional_od_intake_service import (
    RegionalOdIntakeError,
    validate_regional_od_intake,
)


FIXTURE = Path(__file__).parent / "fixtures/regional_od"


def _campaign(tmp_path: Path) -> Path:
    target = tmp_path / "campaign"
    shutil.copytree(FIXTURE, target)
    return target


def _manifest(campaign: Path) -> dict[str, object]:
    return json.loads((campaign / "campaign-manifest.json").read_text(encoding="utf-8"))


def _write_manifest(campaign: Path, manifest: dict[str, object]) -> None:
    (campaign / "campaign-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def _refresh_od_identity(campaign: Path, manifest: dict[str, object]) -> None:
    path = campaign / "od.csv"
    contents = path.read_bytes()
    entry = manifest["files"]["od_table"]  # type: ignore[index]
    entry["bytes"] = len(contents)
    entry["sha256"] = hashlib.sha256(contents).hexdigest()


def test_valid_delivery_normalizes_units_zones_and_release_boundary(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)

    od, zones, report = validate_regional_od_intake(campaign)

    assert od is not None and zones is not None
    assert report["status"] == "assignment_ready"
    assert report["normalization_ready"] is True
    assert report["assignment_ready"] is True
    assert report["publication_ready"] is True
    assert set(zones["zone_type"]) == {"internal", "external"}
    assert report["quality"]["periods"]["weekday_morning"]["vehicle_trips"] == 2850.0
    assert report["quality"]["periods"]["weekday_afternoon"]["vehicle_trips"] == 2750.0
    assert od.loc[od["period"] == "weekday_morning", "period_duration_minutes"].eq(120).all()


def test_duplicate_od_keys_block_normalized_output(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    manifest = _manifest(campaign)
    with (campaign / "od.csv").open("a", encoding="utf-8") as handle:
        handle.write("Z1,Z2,AM,25,SOV\n")
    _refresh_od_identity(campaign, manifest)
    _write_manifest(campaign, manifest)

    od, zones, report = validate_regional_od_intake(campaign)

    assert od is None and zones is None
    assert report["status"] == "blocked"
    assert report["quality"]["canonical_duplicate_key_count"] == 1
    assert any("duplicate canonical keys" in item for item in report["blocking_issues"])


def test_unknown_trip_units_and_release_terms_block_intake(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    manifest = _manifest(campaign)
    manifest["units"]["trip_measure"] = "person_trips"  # type: ignore[index]
    manifest["units"]["value_basis"] = "unknown"  # type: ignore[index]
    manifest["license"]["internal_model_use_authorized"] = False  # type: ignore[index]
    manifest["license"]["source_data_redistribution"] = "unknown"  # type: ignore[index]
    _write_manifest(campaign, manifest)

    od, zones, report = validate_regional_od_intake(campaign)

    assert od is None and zones is None
    assert report["normalization_ready"] is False
    assert any("vehicle_trips" in item for item in report["blocking_issues"])
    assert any("Internal model use" in item for item in report["blocking_issues"])
    assert any("redistribution permission" in item for item in report["blocking_issues"])


def test_orphan_zone_ids_block_assignment(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    manifest = _manifest(campaign)
    path = campaign / "od.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace("X1,Z1,AM", "MISSING,Z1,AM"),
        encoding="utf-8",
    )
    _refresh_od_identity(campaign, manifest)
    _write_manifest(campaign, manifest)

    od, zones, report = validate_regional_od_intake(campaign)

    assert od is None and zones is None
    assert any("orphan origins" in item for item in report["blocking_issues"])


def test_unknown_bistate_overlap_allows_normalization_but_not_assignment(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    manifest = _manifest(campaign)
    manifest["boundary_overlap"] = {"status": "unknown", "reference": ""}
    _write_manifest(campaign, manifest)

    od, zones, report = validate_regional_od_intake(campaign)

    assert od is not None and zones is not None
    assert report["status"] == "validated_not_assignment_ready"
    assert report["normalization_ready"] is True
    assert report["assignment_ready"] is False
    assert report["publication_ready"] is False


def test_checksum_mismatch_stops_before_parsing(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    path = campaign / "od.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Z1,Z2,AM,1200", "Z1,Z2,AM,1201"),
        encoding="utf-8",
    )

    with pytest.raises(RegionalOdIntakeError, match="SHA-256 mismatch"):
        validate_regional_od_intake(campaign)
