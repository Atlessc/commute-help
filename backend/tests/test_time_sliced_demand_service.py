"""Phase 3.2 bucket-contained SUMO demand generation gates."""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.schemas.dynamic_od import DynamicOdRow
from backend.app.services.dynamic_od_service import (
    finalize_dynamic_od_payload,
    write_dynamic_od_artifact_atomic,
)
from backend.app.services.sumo.time_sliced_demand_service import (
    TimeSlicedDemandError,
    balanced_stochastic_round,
    build_time_sliced_demand,
    load_time_sliced_demand_artifact,
)

PHASE_3_1_FIXTURE = (
    Path(__file__).parent / "fixtures/dynamic_od/two-hour-draft.json"
)
GENERATED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _source(tmp_path: Path) -> Path:
    payload = json.loads(PHASE_3_1_FIXTURE.read_text(encoding="utf-8"))
    artifact = finalize_dynamic_od_payload(payload)
    path = tmp_path / "dynamic-od.json"
    write_dynamic_od_artifact_atomic(path, artifact)
    return path


def _build(tmp_path: Path, name: str, *, seed: int | None = None):
    source = _source(tmp_path)
    return build_time_sliced_demand(
        source_artifact_path=source,
        output_directory=tmp_path / name,
        weekday="Monday",
        seed_override=seed,
        generated_at=GENERATED_AT,
    )


def _trips(path: Path) -> list[ET.Element]:
    with gzip.open(path / "time-sliced-demand.trips.xml.gz", "rb") as handle:
        return ET.fromstring(handle.read()).findall("trip")


def test_two_hour_fixture_generates_all_eight_exact_buckets(tmp_path: Path) -> None:
    manifest = _build(tmp_path, "output")

    assert manifest.bucket_start_minutes == tuple(range(420, 540, 15))
    assert len(manifest.bucket_audits) == 8
    assert manifest.generated_vehicle_count == 2_160
    assert manifest.rounding_audit.target_vehicle_trips == 2_160
    assert manifest.represented_target_vehicle_trips == 2_160
    assert manifest.weekday == "Monday"


def test_all_departures_remain_inside_half_open_source_bucket(tmp_path: Path) -> None:
    manifest = _build(tmp_path, "output")
    trips = _trips(tmp_path / "output")

    assert manifest.departure_audit.leakage_count == 0
    assert manifest.departure_audit.departure_at_interval_end_count == 0
    assert len(trips) == 2_160
    for trip in trips:
        parts = str(trip.get("id")).split("-")
        bucket = int(parts[2])
        departure = float(str(trip.get("depart")))
        assert bucket * 60 <= departure < bucket * 60 + 900


def test_integer_fixture_conserves_each_bucket_movement_and_vehicle_class(
    tmp_path: Path,
) -> None:
    manifest = _build(tmp_path, "output")

    for bucket in manifest.bucket_audits:
        assert bucket.generated_vehicle_count == bucket.target_vehicle_trips
        assert bucket.rounding_error_vehicle_trips == 0
        assert bucket.maximum_absolute_row_error_vehicle_trips == 0
        assert bucket.departure_leakage_count == 0
        assert all(
            item.rounding_error_vehicle_trips == 0
            for item in bucket.movement_class_conservation
        )
        assert all(
            item.rounding_error_vehicle_trips == 0
            for item in bucket.vehicle_class_conservation
        )


def test_balanced_fractional_rounding_bounds_rows_and_whole_bucket() -> None:
    source_payload = json.loads(PHASE_3_1_FIXTURE.read_text(encoding="utf-8"))
    artifact = finalize_dynamic_od_payload(source_payload)
    original = tuple(row for row in artifact.rows if row.bucket_start_minute == 420)
    fractional: list[DynamicOdRow] = []
    adjustments = (0.25, 0.75, -0.25, -0.75)
    for row, adjustment in zip(original, adjustments, strict=True):
        payload = row.model_dump(mode="json")
        payload["target_vehicle_trips"] += adjustment
        payload["target_flow_vph"] = payload["target_vehicle_trips"] * 4
        fractional.append(DynamicOdRow.model_validate(payload))

    counts, errors, aggregate_error = balanced_stochastic_round(
        tuple(fractional), seed=90210
    )

    assert sum(row.target_vehicle_trips for row in fractional) == 200
    assert sum(counts) == 200
    assert abs(aggregate_error) < 1
    assert all(abs(error) < 1 for error in errors)
    assert any(error != 0 for error in errors)


def test_rounding_never_moves_fractional_trips_between_buckets(tmp_path: Path) -> None:
    payload = json.loads(PHASE_3_1_FIXTURE.read_text(encoding="utf-8"))
    first = next(row for row in payload["rows"] if row["bucket_start_minute"] == 420)
    second = next(row for row in payload["rows"] if row["bucket_start_minute"] == 435)
    first["target_vehicle_trips"] += 0.4
    first["target_flow_vph"] = first["target_vehicle_trips"] * 4
    second["target_vehicle_trips"] -= 0.4
    second["target_flow_vph"] = second["target_vehicle_trips"] * 4
    source = tmp_path / "fractional.json"
    write_dynamic_od_artifact_atomic(source, finalize_dynamic_od_payload(payload))

    manifest = build_time_sliced_demand(
        source_artifact_path=source,
        output_directory=tmp_path / "output",
        generated_at=GENERATED_AT,
    )

    assert sum(audit.target_vehicle_trips for audit in manifest.bucket_audits) == 2_160
    assert all(abs(audit.rounding_error_vehicle_trips) < 1 for audit in manifest.bucket_audits)
    assert manifest.departure_audit.leakage_count == 0


def test_same_source_and_seed_produce_identical_demand_and_identity(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path, "first")
    second = _build(tmp_path, "second")

    assert first.content_digest == second.content_digest
    assert first.output.sha256 == second.output.sha256
    assert first.output.uncompressed_sha256 == second.output.uncompressed_sha256
    assert (tmp_path / "first/time-sliced-demand.trips.xml.gz").read_bytes() == (
        tmp_path / "second/time-sliced-demand.trips.xml.gz"
    ).read_bytes()


def test_changed_seed_changes_placement_not_target_or_conservation(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path, "first", seed=31001)
    second = _build(tmp_path, "second", seed=31002)

    assert first.output.sha256 != second.output.sha256
    assert first.content_digest != second.content_digest
    assert first.generated_vehicle_count == second.generated_vehicle_count == 2_160
    assert first.rounding_audit.target_vehicle_trips == (
        second.rounding_audit.target_vehicle_trips
    )
    assert [audit.generated_vehicle_count for audit in first.bucket_audits] == [
        audit.generated_vehicle_count for audit in second.bucket_audits
    ]
    assert first.departure_audit.leakage_count == second.departure_audit.leakage_count == 0


def test_phase_three_one_lineage_and_one_to_one_semantics_survive(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source_payload = json.loads(source.read_text(encoding="utf-8"))
    manifest = build_time_sliced_demand(
        source_artifact_path=source,
        output_directory=tmp_path / "output",
        generated_at=GENERATED_AT,
    )

    assert manifest.source_dynamic_od_content_digest == source_payload["content_digest"]
    assert manifest.source_demand_version == source_payload["demand_version"]
    assert set(manifest.source_evidence_content_digests) == {
        row["content_digest"] for row in source_payload["evidence_sources"]
    }
    assert manifest.real_vehicles_per_modeled_vehicle == 1.0
    assert manifest.calibration_status == "not_calibrated"
    assert manifest.routing_status == "unrouted_zone_pair_demand"


def test_weekday_substitution_and_weekends_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path)

    with pytest.raises(TimeSlicedDemandError, match="substitution is prohibited"):
        build_time_sliced_demand(
            source_artifact_path=source,
            output_directory=tmp_path / "tuesday",
            weekday="Tuesday",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(TimeSlicedDemandError, match="substitution is prohibited"):
        build_time_sliced_demand(
            source_artifact_path=source,
            output_directory=tmp_path / "saturday",
            weekday="Saturday",
            generated_at=GENERATED_AT,
        )


def test_output_identity_contains_no_selected_trip_fields(tmp_path: Path) -> None:
    manifest = _build(tmp_path, "output")
    encoded = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)

    assert "selected_trip" not in encoded
    assert "trip_probe" not in encoded
    assert "origin_app_edge_id" not in encoded
    assert "destination_app_edge_id" not in encoded


def test_output_is_atomic_and_integrity_checked(tmp_path: Path) -> None:
    manifest = _build(tmp_path, "output")
    output = tmp_path / "output"

    assert not (tmp_path / ".output.pending").exists()
    assert load_time_sliced_demand_artifact(output) == manifest
    demand_path = output / "time-sliced-demand.trips.xml.gz"
    demand_path.write_bytes(demand_path.read_bytes() + b"corrupt")
    with pytest.raises(TimeSlicedDemandError, match="output hash"):
        load_time_sliced_demand_artifact(output)


def test_existing_output_and_malformed_phase_three_one_input_are_rejected(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "output"
    build_time_sliced_demand(
        source_artifact_path=source,
        output_directory=output,
        generated_at=GENERATED_AT,
    )
    with pytest.raises(TimeSlicedDemandError, match="already exists"):
        build_time_sliced_demand(
            source_artifact_path=source,
            output_directory=output,
            generated_at=GENERATED_AT,
        )

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError):
        build_time_sliced_demand(
            source_artifact_path=malformed,
            output_directory=tmp_path / "malformed-output",
            generated_at=GENERATED_AT,
        )
