"""Focused Phase 1.1 tests for the calibration-v2 artifact contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.schemas.calibration_v2 import (
    CALIBRATION_V2_SCHEMA_VERSION,
    CalibrationArtifactV2,
    DerivedProfileRecord,
    ObservationRecord,
)
from backend.app.services.calibration_v2_artifact_service import (
    calibration_artifact_sha256,
    canonical_calibration_artifact_json,
    load_calibration_artifact,
    parse_calibration_artifact,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "calibration_v2"
    / "tiny-calibration-v2.json"
)


def _payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _record(payload: dict, record_id: str) -> dict:
    return next(record for record in payload["records"] if record["record_id"] == record_id)


def test_tiny_calibration_v2_fixture_parses_with_two_weekdays_and_intervals() -> None:
    artifact = load_calibration_artifact(FIXTURE_PATH)

    observations = [
        record for record in artifact.records if isinstance(record, ObservationRecord)
    ]

    assert artifact.schema_version == CALIBRATION_V2_SCHEMA_VERSION
    assert artifact.source_references[0].relative_path == (
        "synthetic/tiny-manifest.json"
    )
    assert {record.calendar.weekday for record in observations} == {"monday", "tuesday"}
    assert {record.calendar.interval_start.minute for record in observations} == {0, 15}
    assert {record.direction for record in observations} == {"northbound", "eastbound"}
    assert artifact.calibration_status == "not_calibrated"


@pytest.mark.parametrize("schema_version", [None, 1, 3, "2"])
def test_schema_version_is_required_and_exact(schema_version: object) -> None:
    payload = _payload()
    if schema_version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema_version

    with pytest.raises(ValidationError):
        CalibrationArtifactV2.model_validate(payload)


def test_calendar_direction_and_provenance_survive_round_trip() -> None:
    artifact = load_calibration_artifact(FIXTURE_PATH)
    canonical = canonical_calibration_artifact_json(artifact)
    restored = parse_calibration_artifact(canonical)

    monday = next(
        record
        for record in restored.records
        if record.record_id == "obs.synthetic-station-a.northbound.2026-01-05t0700"
    )
    profile = next(
        record
        for record in restored.records
        if record.record_id == "profile.synthetic-edge-101.northbound.monday.0700"
    )

    assert isinstance(monday, ObservationRecord)
    assert monday.calendar.weekday == "monday"
    assert monday.calendar.exact_date.isoformat() == "2026-01-05"
    assert monday.direction == "northbound"
    assert monday.detector.station_id == "SYN-STATION-A"
    assert monday.road_association is not None
    assert monday.road_association.accepted_sumo_association is not None
    assert monday.road_association.accepted_sumo_association.mapping_version == (
        "synthetic-map-v1"
    )

    assert isinstance(profile, DerivedProfileRecord)
    assert profile.calendar.weekday == "monday"
    assert profile.calendar.profile.profile_id == "synthetic.weekday-winter"
    assert profile.provenance.source_record_ids == (
        "obs.synthetic-station-a.northbound.2026-01-05t0700",
    )
    assert profile.provenance.source_window_start.isoformat() == "2026-01-05"
    assert profile.provenance.sample_days == 1


def test_exact_date_and_profile_calendar_identities_cannot_be_conflated() -> None:
    payload = _payload()
    observation = _record(
        payload, "obs.synthetic-station-a.northbound.2026-01-05t0700"
    )
    profile = _record(
        payload, "profile.synthetic-edge-101.northbound.monday.0700"
    )

    observation["calendar"] = copy.deepcopy(profile["calendar"])

    with pytest.raises(ValidationError):
        CalibrationArtifactV2.model_validate(payload)


@pytest.mark.parametrize(
    ("exact_date", "weekday"),
    [
        ("2026-01-05", "tuesday"),
        ("2026-01-10", "monday"),
        ("2026-01-05", "mon_thu"),
        ("2026-01-05", "saturday"),
    ],
)
def test_invalid_calendar_and_weekday_values_are_rejected(
    exact_date: str, weekday: str
) -> None:
    payload = _payload()
    record = _record(
        payload, "obs.synthetic-station-a.northbound.2026-01-05t0700"
    )
    record["calendar"]["exact_date"] = exact_date
    record["calendar"]["weekday"] = weekday
    if exact_date == "2026-01-10":
        record["calendar"]["interval_start"] = "2026-01-10T07:00:00-08:00"
        record["calendar"]["interval_end"] = "2026-01-10T07:15:00-08:00"

    with pytest.raises(ValidationError):
        CalibrationArtifactV2.model_validate(payload)


def test_rejected_missing_measurements_are_distinct_from_valid_zero_values() -> None:
    artifact = load_calibration_artifact(FIXTURE_PATH)
    rejected = next(
        record
        for record in artifact.records
        if record.record_id == "obs.synthetic-station-b.eastbound.2026-01-06t0715"
    )
    zero = next(
        record
        for record in artifact.records
        if record.record_id
        == "obs.synthetic-station-b.eastbound.2026-01-06t0700-zero-volume"
    )

    assert isinstance(rejected, ObservationRecord)
    assert rejected.quality.disposition == "rejected"
    assert rejected.measurements.volume_count is None
    assert rejected.measurements.flow_vph is None
    assert "volume_count" in rejected.quality.missing_fields
    assert rejected.quality.exclusion_reasons == ("all-measurements-missing",)

    assert isinstance(zero, ObservationRecord)
    assert zero.quality.disposition == "accepted"
    assert zero.measurements.volume_count == 0
    assert zero.measurements.flow_vph == 0
    assert "volume_count" not in zero.quality.missing_fields


def test_historical_input_cannot_become_historically_calibrated_by_label() -> None:
    payload = _payload()
    payload["artifact_status"] = "historical_input"
    payload["source_dataset"]["synthetic"] = False
    payload["calibration_status"] = "historically_calibrated"

    with pytest.raises(ValidationError):
        CalibrationArtifactV2.model_validate(payload)

    payload = _payload()
    payload["records"][0]["evidence"]["calibration_status"] = (
        "historically_calibrated"
    )
    with pytest.raises(ValidationError):
        CalibrationArtifactV2.model_validate(payload)


def test_canonical_serialization_and_hash_are_deterministic() -> None:
    first = load_calibration_artifact(FIXTURE_PATH)
    reversed_payload = _payload()
    reversed_payload["records"].reverse()
    second = CalibrationArtifactV2.model_validate(reversed_payload)

    first_json = canonical_calibration_artifact_json(first)
    second_json = canonical_calibration_artifact_json(second)

    assert first_json == second_json
    assert parse_calibration_artifact(first_json) == first
    assert canonical_calibration_artifact_json(parse_calibration_artifact(first_json)) == (
        first_json
    )
    assert calibration_artifact_sha256(first) == calibration_artifact_sha256(second)
