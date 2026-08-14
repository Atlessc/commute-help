"""Phase 3.1 dynamic OD schema, identity, and two-hour gate."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.schemas.dynamic_od import DynamicOdArtifactV1
from backend.app.services.dynamic_od_service import (
    DynamicOdContractError,
    finalize_dynamic_od_payload,
    load_dynamic_od_artifact,
    write_dynamic_od_artifact_atomic,
)

FIXTURE = Path(__file__).parent / "fixtures/dynamic_od/two-hour-draft.json"


def _draft() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _finalized() -> DynamicOdArtifactV1:
    return finalize_dynamic_od_payload(_draft())


def test_two_hour_fixture_has_eight_distinct_monday_matrices() -> None:
    artifact = _finalized()

    assert artifact.weekdays == ("Monday",)
    assert artifact.bucket_start_minutes == tuple(range(420, 540, 15))
    assert len(artifact.conservation) == 8
    assert len(artifact.rows) == 32
    assert {row.weekday.value for row in artifact.rows} == {"Monday"}
    assert all(row.bucket_start_minute % 15 == 0 for row in artifact.rows)


def test_gateway_movement_classes_and_vehicle_classes_remain_explicit() -> None:
    artifact = _finalized()

    assert {row.movement_class.value for row in artifact.rows} == {
        "internal_internal",
        "internal_gateway",
        "gateway_internal",
        "gateway_gateway",
    }
    assert {row.vehicle_class for row in artifact.rows} == {
        "passenger_sov",
        "heavy_truck",
    }


def test_each_bucket_conserves_rows_movement_totals_and_vph() -> None:
    artifact = _finalized()

    for audit in artifact.conservation:
        rows = [
            row
            for row in artifact.rows
            if row.weekday == audit.weekday
            and row.bucket_start_minute == audit.bucket_start_minute
        ]
        assert audit.row_count == len(rows) == 4
        assert audit.target_vehicle_trips == sum(
            row.target_vehicle_trips for row in rows
        )
        assert audit.target_flow_vph == audit.target_vehicle_trips * 4
        assert sum(item.target_vehicle_trips for item in audit.movement_totals) == (
            audit.target_vehicle_trips
        )
        assert sum(item.target_flow_vph for item in audit.movement_totals) == (
            audit.target_flow_vph
        )


def test_one_to_one_and_seed_lineage_are_declared_not_applied() -> None:
    artifact = _finalized()

    assert artifact.vehicle_semantics.real_vehicles_per_modeled_vehicle == 1.0
    assert artifact.vehicle_semantics.semantics == (
        "one_sumo_vehicle_per_modeled_vehicle_trip"
    )
    assert artifact.seed_policy.base_seed == 31001
    assert artifact.seed_policy.application_status == (
        "declared_for_phase-3.2_not_applied"
    )
    assert artifact.assignment_policy.path_policy_status == "deferred_to_phase-3.2"


def test_frozen_phase_one_lineage_is_required() -> None:
    payload = _draft()
    payload["evidence_sources"] = [
        row
        for row in payload["evidence_sources"]
        if row["role"] != "historical_quality_policy"
    ]

    with pytest.raises(DynamicOdContractError, match="frozen corpus"):
        finalize_dynamic_od_payload(payload)


def test_changed_adjacent_target_requires_bucket_detector_evidence() -> None:
    payload = _draft()
    changed = next(
        row
        for row in payload["rows"]
        if row["bucket_start_minute"] == 435
        and row["movement_class"] == "internal_internal"
    )
    changed["adjacent_change_evidence_ids"] = []

    with pytest.raises(DynamicOdContractError, match="changed adjacent OD targets"):
        finalize_dynamic_od_payload(payload)


def test_non_detector_evidence_cannot_justify_adjacent_change() -> None:
    payload = _draft()
    changed = next(row for row in payload["rows"] if row["bucket_start_minute"] == 435)
    changed["adjacent_change_evidence_ids"] = ["phase-1.3-profiles"]

    with pytest.raises(DynamicOdContractError, match="bucket-specific detector"):
        finalize_dynamic_od_payload(payload)


def test_weekday_pooling_and_fallbacks_are_explicitly_prohibited() -> None:
    artifact = _finalized()

    assert artifact.fallback_policy.weekday_pooling == "prohibited"
    assert artifact.fallback_policy.weekday_substitution == "prohibited"
    assert artifact.fallback_policy.season_substitution == "prohibited"
    assert artifact.fallback_policy.exact_date_substitution == "prohibited"
    assert artifact.fallback_policy.missing_bucket_interpolation == "prohibited"
    assert artifact.temporal_regularization.production_numeric_change_threshold is None
    assert artifact.temporal_regularization.threshold_status == (
        "unresolved_not_invented"
    )


def test_missing_bucket_and_partial_matrix_are_rejected_not_interpolated() -> None:
    missing_bucket = _draft()
    missing_bucket["rows"] = [
        row for row in missing_bucket["rows"] if row["bucket_start_minute"] != 465
    ]
    missing_bucket["bucket_start_minutes"] = [420, 435, 450, 480, 495, 510, 525]
    with pytest.raises(DynamicOdContractError, match="contiguous"):
        finalize_dynamic_od_payload(missing_bucket)

    partial = _draft()
    partial["rows"] = partial["rows"][:-1]
    with pytest.raises(DynamicOdContractError, match="same explicit zone-pair rows"):
        finalize_dynamic_od_payload(partial)


def test_selected_trip_fields_are_not_part_of_regional_demand_contract() -> None:
    payload = _finalized().model_dump(mode="json")
    payload["selected_trip_origin"] = "private-origin"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DynamicOdArtifactV1.model_validate(payload)


def test_deterministic_identity_excludes_generated_at_but_tracks_demand_inputs() -> None:
    first_payload = _draft()
    second_payload = deepcopy(first_payload)
    second_payload["generated_at"] = "2026-08-14T08:00:00-07:00"

    first = finalize_dynamic_od_payload(first_payload)
    second = finalize_dynamic_od_payload(second_payload)
    assert first.content_digest == second.content_digest
    assert [row.row_identity for row in first.rows] == [
        row.row_identity for row in second.rows
    ]

    changed = deepcopy(first_payload)
    changed["seed_policy"]["base_seed"] = 31002
    assert finalize_dynamic_od_payload(changed).content_digest != first.content_digest


def test_atomic_round_trip_is_deterministic_and_leaves_no_pending_file(
    tmp_path: Path,
) -> None:
    artifact = _finalized()
    output = tmp_path / "dynamic-od.json"

    write_dynamic_od_artifact_atomic(output, artifact)
    loaded = load_dynamic_od_artifact(output)

    assert loaded == artifact
    assert not (tmp_path / ".dynamic-od.json.pending").exists()
    first_bytes = output.read_bytes()
    write_dynamic_od_artifact_atomic(output, artifact)
    assert output.read_bytes() == first_bytes


def test_schema_rejects_incorrect_units_and_weekends() -> None:
    wrong_units = _draft()
    wrong_units["rows"][0]["target_flow_vph"] = 401
    with pytest.raises(ValidationError, match="multiplied by 4"):
        finalize_dynamic_od_payload(wrong_units)

    weekend = _draft()
    weekend["rows"][0]["weekday"] = "Saturday"
    with pytest.raises(ValidationError):
        finalize_dynamic_od_payload(weekend)


def test_fixture_timestamp_is_timezone_aware() -> None:
    artifact = _finalized()

    assert isinstance(artifact.generated_at, datetime)
    assert artifact.generated_at.utcoffset() is not None
