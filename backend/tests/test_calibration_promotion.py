"""Focused Phase 2.3 explicit promotion lifecycle tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.schemas.calibration_promotion import (
    CriteriaPolicyStatus,
    PromotionCandidateIdentity,
    PromotionCandidateKind,
    PromotionCriteriaPolicy,
    PromotionCriterionResult,
    PromotionEvidenceLineage,
    PromotionFailureCode,
    PromotionFailureReason,
    PromotionLifecycleState,
)
from backend.app.services import calibration_promotion_service as service
from backend.app.services.calibration_promotion_service import (
    PromotionDecisionError,
    load_promotion_artifact,
    materialize_candidate_registration,
    materialize_lifecycle_transition,
)

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "calibration_promotion"
FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def test_passing_fixture_traverses_every_required_state(tmp_path: Path) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    candidate, evidence, policy = _lineage(values)
    candidate_decision, _ = _register(tmp_path / "00-candidate", values)
    calibration, _ = materialize_lifecycle_transition(
        output_directory=tmp_path / "01-calibration-passed",
        previous_decision_path=tmp_path / "00-candidate",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.CALIBRATION_PASSED,
        decision_author="synthetic-fixture-runner",
        criterion_results=_results(values, "calibration"),
        generated_at=FIXED_TIME,
    )
    validation, _ = materialize_lifecycle_transition(
        output_directory=tmp_path / "02-validation-passed",
        previous_decision_path=tmp_path / "01-calibration-passed",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.VALIDATION_PASSED,
        decision_author="synthetic-fixture-runner",
        criterion_results=_results(values, "validation"),
        generated_at=FIXED_TIME,
    )
    promoted, manifest = materialize_lifecycle_transition(
        output_directory=tmp_path / "03-promoted",
        previous_decision_path=tmp_path / "02-validation-passed",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.PROMOTED,
        decision_author="synthetic-fixture-runner",
        generated_at=FIXED_TIME,
    )

    assert [
        candidate_decision.transition.to_state,
        calibration.transition.to_state,
        validation.transition.to_state,
        promoted.transition.to_state,
    ] == [
        PromotionLifecycleState.CANDIDATE,
        PromotionLifecycleState.CALIBRATION_PASSED,
        PromotionLifecycleState.VALIDATION_PASSED,
        PromotionLifecycleState.PROMOTED,
    ]
    assert calibration.previous_decision_digest == candidate_decision.content_digest
    assert validation.previous_decision_digest == calibration.content_digest
    assert promoted.previous_decision_digest == validation.content_digest
    assert manifest.lifecycle_state == PromotionLifecycleState.PROMOTED


def test_failing_fixture_is_rejected_with_inspectable_reason(tmp_path: Path) -> None:
    values = _fixture("failing-synthetic-evaluation.json")
    candidate, evidence, policy = _lineage(values)
    _register(tmp_path / "candidate", values)
    rejected, _ = materialize_lifecycle_transition(
        output_directory=tmp_path / "rejected",
        previous_decision_path=tmp_path / "candidate",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.REJECTED,
        decision_author="synthetic-fixture-runner",
        criterion_results=_results(values, "calibration"),
        failure_reasons=(
            PromotionFailureReason(
                code=PromotionFailureCode.CALIBRATION_METRIC_FAILED,
                criterion_id="synthetic.calibration_score",
                detail="The deliberately failing synthetic criterion did not pass.",
            ),
        ),
        generated_at=FIXED_TIME,
    )

    persisted, _ = load_promotion_artifact(tmp_path / "rejected")
    assert rejected.transition.to_state == PromotionLifecycleState.REJECTED
    assert persisted.failure_reasons[0].code == (
        PromotionFailureCode.CALIBRATION_METRIC_FAILED
    )
    assert not (tmp_path / "promoted").exists()
    with pytest.raises(PromotionDecisionError) as caught:
        materialize_lifecycle_transition(
            output_directory=tmp_path / "promoted",
            previous_decision_path=tmp_path / "rejected",
            candidate=candidate,
            evidence=evidence,
            criteria_policy=policy,
            to_state=PromotionLifecycleState.PROMOTED,
            decision_author="synthetic-fixture-runner",
            generated_at=FIXED_TIME,
        )
    assert caught.value.code == PromotionFailureCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("measurement_complete", PromotionFailureCode.INCOMPLETE_MEASUREMENT),
        ("integrity_check_passed", PromotionFailureCode.INTEGRITY_FAILURE),
    ],
)
def test_missing_or_failed_evidence_cannot_pass_calibration(
    tmp_path: Path, field: str, code: PromotionFailureCode
) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    candidate, evidence, policy = _lineage(values)
    evidence = evidence.model_copy(update={field: False})
    materialize_candidate_registration(
        output_directory=tmp_path / "candidate",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        decision_author="synthetic-fixture-runner",
        generated_at=FIXED_TIME,
    )
    with pytest.raises(PromotionDecisionError) as caught:
        materialize_lifecycle_transition(
            output_directory=tmp_path / "calibration",
            previous_decision_path=tmp_path / "candidate",
            candidate=candidate,
            evidence=evidence,
            criteria_policy=policy,
            to_state=PromotionLifecycleState.CALIBRATION_PASSED,
            decision_author="synthetic-fixture-runner",
            criterion_results=_results(values, "calibration"),
            generated_at=FIXED_TIME,
        )
    assert caught.value.code == code
    assert not (tmp_path / "calibration").exists()


def test_incompatible_lineage_cannot_advance(tmp_path: Path) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    candidate, evidence, policy = _lineage(values)
    _register(tmp_path / "candidate", values)
    changed = candidate.model_copy(
        update={
            "artifact": candidate.artifact.model_copy(
                update={"content_digest": "1" * 64}
            )
        }
    )
    with pytest.raises(PromotionDecisionError) as caught:
        materialize_lifecycle_transition(
            output_directory=tmp_path / "calibration",
            previous_decision_path=tmp_path / "candidate",
            candidate=changed,
            evidence=evidence,
            criteria_policy=policy,
            to_state=PromotionLifecycleState.CALIBRATION_PASSED,
            decision_author="synthetic-fixture-runner",
            criterion_results=_results(values, "calibration"),
            generated_at=FIXED_TIME,
        )
    assert caught.value.code == PromotionFailureCode.INCOMPATIBLE_LINEAGE


@pytest.mark.parametrize(
    ("start_state", "illegal_target"),
    [
        (PromotionLifecycleState.CANDIDATE, PromotionLifecycleState.PROMOTED),
        (
            PromotionLifecycleState.CALIBRATION_PASSED,
            PromotionLifecycleState.PROMOTED,
        ),
        (PromotionLifecycleState.PROMOTED, PromotionLifecycleState.CANDIDATE),
        (PromotionLifecycleState.PROMOTED, PromotionLifecycleState.REJECTED),
    ],
)
def test_illegal_state_transitions_are_rejected(
    tmp_path: Path,
    start_state: PromotionLifecycleState,
    illegal_target: PromotionLifecycleState,
) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    candidate, evidence, policy = _lineage(values)
    paths = _passing_chain_to(tmp_path, values, start_state)
    with pytest.raises(PromotionDecisionError) as caught:
        materialize_lifecycle_transition(
            output_directory=tmp_path / "illegal",
            previous_decision_path=paths[-1],
            candidate=candidate,
            evidence=evidence,
            criteria_policy=policy,
            to_state=illegal_target,
            decision_author="synthetic-fixture-runner",
            generated_at=FIXED_TIME,
        )
    assert caught.value.code == PromotionFailureCode.ILLEGAL_STATE_TRANSITION


def test_promoted_decision_is_frozen_and_cannot_be_mutated(tmp_path: Path) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    promoted_path = _passing_chain_to(
        tmp_path, values, PromotionLifecycleState.PROMOTED
    )[-1]
    promoted, _ = load_promotion_artifact(promoted_path)
    with pytest.raises(ValidationError):
        promoted.decision_author = "rewritten"  # type: ignore[misc]


def test_identical_evaluation_is_idempotent_and_identity_sensitive(
    tmp_path: Path,
) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    first, first_manifest = _register(tmp_path / "candidate", values)
    second, second_manifest = materialize_candidate_registration(
        output_directory=tmp_path / "candidate",
        candidate=first.candidate,
        evidence=first.evidence,
        criteria_policy=first.criteria_policy,
        decision_author=first.decision_author,
        generated_at=FIXED_TIME + timedelta(days=1),
    )
    changed = first.candidate.model_copy(
        update={
            "artifact": first.candidate.artifact.model_copy(
                update={"content_digest": "2" * 64}
            )
        }
    )
    changed_decision, _ = materialize_candidate_registration(
        output_directory=tmp_path / "changed",
        candidate=changed,
        evidence=first.evidence,
        criteria_policy=first.criteria_policy,
        decision_author=first.decision_author,
        generated_at=FIXED_TIME,
    )
    assert first.content_digest == second.content_digest
    assert first_manifest.content_digest == second_manifest.content_digest
    assert changed_decision.content_digest != first.content_digest


def test_unresolved_production_criteria_cannot_advance(tmp_path: Path) -> None:
    values = _fixture("passing-synthetic-evaluation.json")
    candidate, evidence, _ = _lineage(values)
    candidate = candidate.model_copy(
        update={"candidate_kind": PromotionCandidateKind.CALIBRATION_ARTIFACT}
    )
    policy = PromotionCriteriaPolicy(
        policy_name="traffic_accuracy_criteria_pending_review",
        policy_version="phase-2.3-unresolved-v1",
        policy_status=CriteriaPolicyStatus.UNRESOLVED_PRODUCTION,
        criteria=(),
    )
    materialize_candidate_registration(
        output_directory=tmp_path / "candidate",
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        decision_author="production-registration",
        generated_at=FIXED_TIME,
    )
    with pytest.raises(PromotionDecisionError) as caught:
        materialize_lifecycle_transition(
            output_directory=tmp_path / "calibration",
            previous_decision_path=tmp_path / "candidate",
            candidate=candidate,
            evidence=evidence,
            criteria_policy=policy,
            to_state=PromotionLifecycleState.CALIBRATION_PASSED,
            decision_author="production-registration",
            generated_at=FIXED_TIME,
        )
    assert caught.value.code == PromotionFailureCode.PRODUCTION_CRITERIA_UNRESOLVED


def test_regional_world_identity_rejects_trip_od_fields() -> None:
    payload = _fixture("passing-synthetic-evaluation.json")["candidate"]
    payload["candidate_kind"] = "regional_world_candidate"
    payload["origin"] = "private-origin"
    payload["destination"] = "private-destination"
    with pytest.raises(ValidationError):
        PromotionCandidateIdentity.model_validate(payload)


def test_atomic_failure_cannot_promote_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _fixture("passing-synthetic-evaluation.json")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(service.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        _register(tmp_path / "candidate", values)
    assert not (tmp_path / "candidate").exists()
    assert not (tmp_path / ".candidate.pending").exists()


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))


def _lineage(
    values: dict,
) -> tuple[
    PromotionCandidateIdentity,
    PromotionEvidenceLineage,
    PromotionCriteriaPolicy,
]:
    return (
        PromotionCandidateIdentity.model_validate(values["candidate"]),
        PromotionEvidenceLineage.model_validate(values["evidence"]),
        PromotionCriteriaPolicy.model_validate(values["criteria_policy"]),
    )


def _results(values: dict, phase: str) -> tuple[PromotionCriterionResult, ...]:
    return tuple(
        PromotionCriterionResult.model_validate(item)
        for item in values.get("results", {}).get(phase, [])
    )


def _register(output: Path, values: dict):
    candidate, evidence, policy = _lineage(values)
    return materialize_candidate_registration(
        output_directory=output,
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        decision_author="synthetic-fixture-runner",
        generated_at=FIXED_TIME,
    )


def _passing_chain_to(
    root: Path, values: dict, state: PromotionLifecycleState
) -> list[Path]:
    candidate, evidence, policy = _lineage(values)
    paths = [root / "candidate"]
    _register(paths[0], values)
    if state == PromotionLifecycleState.CANDIDATE:
        return paths
    paths.append(root / "calibration")
    materialize_lifecycle_transition(
        output_directory=paths[-1],
        previous_decision_path=paths[-2],
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.CALIBRATION_PASSED,
        decision_author="synthetic-fixture-runner",
        criterion_results=_results(values, "calibration"),
        generated_at=FIXED_TIME,
    )
    if state == PromotionLifecycleState.CALIBRATION_PASSED:
        return paths
    paths.append(root / "validation")
    materialize_lifecycle_transition(
        output_directory=paths[-1],
        previous_decision_path=paths[-2],
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.VALIDATION_PASSED,
        decision_author="synthetic-fixture-runner",
        criterion_results=_results(values, "validation"),
        generated_at=FIXED_TIME,
    )
    if state == PromotionLifecycleState.VALIDATION_PASSED:
        return paths
    paths.append(root / "promoted")
    materialize_lifecycle_transition(
        output_directory=paths[-1],
        previous_decision_path=paths[-2],
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        to_state=PromotionLifecycleState.PROMOTED,
        decision_author="synthetic-fixture-runner",
        generated_at=FIXED_TIME,
    )
    return paths
