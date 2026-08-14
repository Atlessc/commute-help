"""Phase 2.3 explicit, immutable calibration promotion decisions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.schemas.calibration_promotion import (
    PROMOTION_ALGORITHM,
    PROMOTION_PRODUCER_NAME,
    PROMOTION_PRODUCER_VERSION,
    PROMOTION_SCHEMA_VERSION,
    ComparatorMeasurementLineage,
    CriteriaPolicyStatus,
    CriterionOutcome,
    CriterionPhase,
    PromotionArtifactIdentity,
    PromotionCandidateIdentity,
    PromotionCandidateKind,
    PromotionCriteriaPolicy,
    PromotionCriterionDefinition,
    PromotionCriterionResult,
    PromotionDecisionKind,
    PromotionDecisionV1,
    PromotionEvidenceLineage,
    PromotionFailureCode,
    PromotionFailureReason,
    PromotionLifecycleState,
    PromotionManifestV1,
    PromotionTransition,
)
from backend.app.schemas.sumo_historical_comparison_v2 import ComparatorV2Manifest
from backend.app.services.portal_calibration_v2_importer import canonical_json

PROMOTION_DECISION_FILE = "promotion-decision.json"
PROMOTION_MANIFEST_FILE = "promotion-manifest.json"
UNRESOLVED_PRODUCTION_POLICY_NAME = "traffic_accuracy_criteria_pending_review"
UNRESOLVED_PRODUCTION_POLICY_VERSION = "phase-2.3-unresolved-v1"


class PromotionDecisionError(RuntimeError):
    """A requested lifecycle decision is invalid or cannot be proven."""

    def __init__(self, code: PromotionFailureCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def materialize_candidate_registration(
    *,
    output_directory: Path,
    candidate: PromotionCandidateIdentity,
    evidence: PromotionEvidenceLineage,
    criteria_policy: PromotionCriteriaPolicy,
    decision_author: str,
    generated_at: datetime | None = None,
) -> tuple[PromotionDecisionV1, PromotionManifestV1]:
    """Atomically register one candidate without implying metric success."""
    transition = PromotionTransition(
        from_state=None,
        to_state=PromotionLifecycleState.CANDIDATE,
        decision_kind=PromotionDecisionKind.CANDIDATE_REGISTRATION,
    )
    return _materialize_decision(
        output_directory=output_directory,
        candidate=candidate,
        evidence=evidence,
        criteria_policy=criteria_policy,
        transition=transition,
        criterion_results=(),
        failure_reasons=(),
        previous_decision_digest=None,
        decision_author=decision_author,
        generated_at=generated_at,
    )


def materialize_lifecycle_transition(
    *,
    output_directory: Path,
    previous_decision_path: Path,
    candidate: PromotionCandidateIdentity,
    evidence: PromotionEvidenceLineage,
    criteria_policy: PromotionCriteriaPolicy,
    to_state: PromotionLifecycleState,
    decision_author: str,
    criterion_results: Iterable[PromotionCriterionResult] = (),
    failure_reasons: Iterable[PromotionFailureReason] = (),
    generated_at: datetime | None = None,
) -> tuple[PromotionDecisionV1, PromotionManifestV1]:
    """Create a new decision version after proving one legal transition."""
    previous, _ = load_promotion_artifact(previous_decision_path)
    _require_same_lineage(previous, candidate, evidence, criteria_policy)
    from_state = previous.transition.to_state
    decision_kind = _decision_kind(from_state, to_state)
    transition = PromotionTransition(
        from_state=from_state,
        to_state=to_state,
        decision_kind=decision_kind,
    )
    results = tuple(criterion_results)
    failures = tuple(failure_reasons)
    _validate_transition_evidence(
        transition=transition,
        candidate=candidate,
        evidence=evidence,
        criteria_policy=criteria_policy,
        criterion_results=results,
        failure_reasons=failures,
    )
    return _materialize_decision(
        output_directory=output_directory,
        candidate=candidate,
        evidence=evidence,
        criteria_policy=criteria_policy,
        transition=transition,
        criterion_results=results,
        failure_reasons=failures,
        previous_decision_digest=previous.content_digest,
        decision_author=decision_author,
        generated_at=generated_at,
    )


def load_promotion_artifact(
    path: Path,
) -> tuple[PromotionDecisionV1, PromotionManifestV1]:
    """Load and independently verify one immutable decision directory."""
    directory = path if path.is_dir() else path.parent
    decision_path = directory / PROMOTION_DECISION_FILE
    manifest_path = directory / PROMOTION_MANIFEST_FILE
    decision = PromotionDecisionV1.model_validate_json(decision_path.read_bytes())
    manifest = PromotionManifestV1.model_validate_json(manifest_path.read_bytes())
    if _decision_content_digest(decision.model_dump(mode="json")) != decision.content_digest:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "promotion decision content digest is invalid",
        )
    if _sha256(decision_path) != manifest.decision_output.sha256:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "promotion decision file hash is invalid",
        )
    if decision_path.stat().st_size != manifest.decision_output.byte_count:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "promotion decision byte count is invalid",
        )
    if (
        manifest.decision_content_digest != decision.content_digest
        or manifest.lifecycle_state != decision.transition.to_state
        or manifest.previous_decision_digest != decision.previous_decision_digest
        or manifest.candidate_content_digest != decision.candidate.artifact.content_digest
        or _manifest_content_digest(manifest.model_dump(mode="json"))
        != manifest.content_digest
    ):
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "promotion manifest lineage is invalid",
        )
    return decision, manifest


def register_comparator_v2_production_candidate(
    *,
    comparator_directory: Path,
    output_directory: Path,
    expected_comparator_digest: str,
    decision_author: str = "phase-2.3-production-registration",
    generated_at: datetime | None = None,
) -> tuple[PromotionDecisionV1, PromotionManifestV1]:
    """Register accepted Comparator-v2 evidence without applying thresholds."""
    comparator_directory = comparator_directory.resolve()
    manifest_path = comparator_directory / "comparator-v2-manifest.json"
    manifest = ComparatorV2Manifest.model_validate_json(manifest_path.read_bytes())
    if manifest.content_digest != expected_comparator_digest:
        raise PromotionDecisionError(
            PromotionFailureCode.INCOMPATIBLE_LINEAGE,
            "Comparator-v2 content digest does not match the accepted gate",
        )
    scoreboard_path = comparator_directory / manifest.scoreboard_output.relative_path
    if _sha256(scoreboard_path) != manifest.scoreboard_output.sha256:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "Comparator-v2 scoreboard hash does not match its manifest",
        )
    scoreboard = _read_json(scoreboard_path)
    integrity = scoreboard.get("integrity")
    if not isinstance(integrity, dict) or any(
        not isinstance(value, int) or value != 0 for value in integrity.values()
    ):
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "Comparator-v2 integrity counters are not all zero",
        )
    candidate = PromotionCandidateIdentity(
        candidate_kind=PromotionCandidateKind.CALIBRATION_ARTIFACT,
        artifact=PromotionArtifactIdentity(
            artifact_type=manifest.artifact_type,
            artifact_id=f"{manifest.application_run_id}:baseline-flow-scoreboard",
            content_digest=manifest.content_digest,
            producer_name=manifest.producer_name,
            producer_version=manifest.producer_version,
        ),
        parent_lineage=(),
    )
    sources = (
        PromotionArtifactIdentity(
            artifact_type="commute_help_historical_station_flow_profiles",
            artifact_id="phase-2.2g-historical-station-flow-profiles",
            content_digest=manifest.station_profile_content_digest,
            producer_name="historical_station_flow_profile_deriver",
            producer_version=manifest.historical_station_profile_version,
        ),
        PromotionArtifactIdentity(
            artifact_type="commute_help_sumo_station_crossing_telemetry",
            artifact_id=manifest.application_run_id,
            content_digest=manifest.station_telemetry_content_digest,
            producer_name="regional_sumo_station_crossing_telemetry",
            producer_version="phase-2.2e-v1",
        ),
        PromotionArtifactIdentity(
            artifact_type="commute_help_historical_observation_corpus",
            artifact_id="portal-calibration-v2-frozen-corpus",
            content_digest=manifest.historical_corpus_digest,
            producer_name="portal_calibration_v2_finalizer",
            producer_version="phase-1.2c2",
        ),
    )
    evidence = PromotionEvidenceLineage(
        comparator=ComparatorMeasurementLineage(
            comparator_name=manifest.producer_name,
            comparator_version=manifest.producer_version,
            comparator_algorithm=manifest.comparator_algorithm,
            content_digest=manifest.content_digest,
            measurement_semantics="physical_station_cross_section_flow_vph",
            evidence_level=manifest.evidence_level,
            calibration_status=manifest.calibration_status,
        ),
        sources=sources,
        integrity_check_passed=True,
        measurement_complete=True,
    )
    policy = PromotionCriteriaPolicy(
        policy_name=UNRESOLVED_PRODUCTION_POLICY_NAME,
        policy_version=UNRESOLVED_PRODUCTION_POLICY_VERSION,
        policy_status=CriteriaPolicyStatus.UNRESOLVED_PRODUCTION,
        criteria=(),
    )
    return materialize_candidate_registration(
        output_directory=output_directory,
        candidate=candidate,
        evidence=evidence,
        criteria_policy=policy,
        decision_author=decision_author,
        generated_at=generated_at,
    )


def _decision_kind(
    from_state: PromotionLifecycleState, to_state: PromotionLifecycleState
) -> PromotionDecisionKind:
    legal = {
        (
            PromotionLifecycleState.CANDIDATE,
            PromotionLifecycleState.CALIBRATION_PASSED,
        ): PromotionDecisionKind.CALIBRATION_EVALUATION,
        (
            PromotionLifecycleState.CALIBRATION_PASSED,
            PromotionLifecycleState.VALIDATION_PASSED,
        ): PromotionDecisionKind.VALIDATION_EVALUATION,
        (
            PromotionLifecycleState.VALIDATION_PASSED,
            PromotionLifecycleState.PROMOTED,
        ): PromotionDecisionKind.PROMOTION_AUTHORIZATION,
        (
            PromotionLifecycleState.CANDIDATE,
            PromotionLifecycleState.REJECTED,
        ): PromotionDecisionKind.REJECTION,
        (
            PromotionLifecycleState.CALIBRATION_PASSED,
            PromotionLifecycleState.REJECTED,
        ): PromotionDecisionKind.REJECTION,
        (
            PromotionLifecycleState.VALIDATION_PASSED,
            PromotionLifecycleState.REJECTED,
        ): PromotionDecisionKind.REJECTION,
    }
    decision_kind = legal.get((from_state, to_state))
    if decision_kind is None:
        raise PromotionDecisionError(
            PromotionFailureCode.ILLEGAL_STATE_TRANSITION,
            f"illegal promotion transition: {from_state.value} -> {to_state.value}",
        )
    return decision_kind


def _require_same_lineage(
    previous: PromotionDecisionV1,
    candidate: PromotionCandidateIdentity,
    evidence: PromotionEvidenceLineage,
    policy: PromotionCriteriaPolicy,
) -> None:
    comparisons = (
        (previous.candidate, candidate, "candidate"),
        (previous.evidence, evidence, "evidence"),
        (previous.criteria_policy, policy, "criteria policy"),
    )
    for old, new, label in comparisons:
        if canonical_json(old.model_dump(mode="json")) != canonical_json(
            new.model_dump(mode="json")
        ):
            raise PromotionDecisionError(
                PromotionFailureCode.INCOMPATIBLE_LINEAGE,
                f"{label} changed within one promotion decision chain",
            )


def _validate_transition_evidence(
    *,
    transition: PromotionTransition,
    candidate: PromotionCandidateIdentity,
    evidence: PromotionEvidenceLineage,
    criteria_policy: PromotionCriteriaPolicy,
    criterion_results: tuple[PromotionCriterionResult, ...],
    failure_reasons: tuple[PromotionFailureReason, ...],
) -> None:
    if transition.to_state == PromotionLifecycleState.REJECTED:
        if not failure_reasons:
            raise PromotionDecisionError(
                PromotionFailureCode.MISSING_REQUIRED_EVIDENCE,
                "rejection requires machine-readable failure reasons",
            )
        criterion_ids = {item.criterion_id for item in criteria_policy.criteria}
        if any(
            item.criterion_id is not None
            and item.criterion_id not in criterion_ids
            for item in failure_reasons
        ):
            raise PromotionDecisionError(
                PromotionFailureCode.INCOMPATIBLE_LINEAGE,
                "rejection references a criterion outside the applied policy",
            )
        _validate_results(criteria_policy, criterion_results, require_pass=False)
        return
    if failure_reasons:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "successful transition cannot carry failure reasons",
        )
    if not evidence.integrity_check_passed:
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "evidence integrity has not passed",
        )
    if not evidence.measurement_complete:
        raise PromotionDecisionError(
            PromotionFailureCode.INCOMPLETE_MEASUREMENT,
            "required measurement evidence is incomplete",
        )
    if criteria_policy.policy_status == CriteriaPolicyStatus.UNRESOLVED_PRODUCTION:
        raise PromotionDecisionError(
            PromotionFailureCode.PRODUCTION_CRITERIA_UNRESOLVED,
            "production traffic-accuracy criteria are not approved",
        )
    if (
        criteria_policy.policy_status == CriteriaPolicyStatus.FIXTURE_LOCAL
        and candidate.candidate_kind != PromotionCandidateKind.SYNTHETIC_FIXTURE
    ):
        raise PromotionDecisionError(
            PromotionFailureCode.INCOMPATIBLE_LINEAGE,
            "fixture-local criteria cannot evaluate a production candidate",
        )
    if transition.to_state == PromotionLifecycleState.CALIBRATION_PASSED:
        _validate_phase_pass(
            criteria_policy, criterion_results, CriterionPhase.CALIBRATION
        )
    elif transition.to_state == PromotionLifecycleState.VALIDATION_PASSED:
        _validate_phase_pass(
            criteria_policy, criterion_results, CriterionPhase.VALIDATION
        )
    elif (
        transition.to_state == PromotionLifecycleState.PROMOTED
        and criterion_results
    ):
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "promotion authorization must rely on prior evaluated decisions",
        )


def _validate_phase_pass(
    policy: PromotionCriteriaPolicy,
    results: tuple[PromotionCriterionResult, ...],
    phase: CriterionPhase,
) -> None:
    definitions = tuple(item for item in policy.criteria if item.phase == phase)
    expected = {item.criterion_id for item in definitions}
    observed = {item.criterion_id for item in results}
    if expected != observed:
        raise PromotionDecisionError(
            PromotionFailureCode.MISSING_REQUIRED_EVIDENCE,
            f"{phase.value} criterion result set is incomplete",
        )
    _validate_results(policy, results, require_pass=True)
    if any(item.phase != phase for item in results):
        raise PromotionDecisionError(
            PromotionFailureCode.INCOMPATIBLE_LINEAGE,
            f"{phase.value} transition contains another phase's result",
        )


def _validate_results(
    policy: PromotionCriteriaPolicy,
    results: tuple[PromotionCriterionResult, ...],
    *,
    require_pass: bool,
) -> None:
    definitions = {item.criterion_id: item for item in policy.criteria}
    if len({item.criterion_id for item in results}) != len(results):
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "criterion result identities are duplicated",
        )
    for result in results:
        definition = definitions.get(result.criterion_id)
        if definition is None or definition.phase != result.phase:
            raise PromotionDecisionError(
                PromotionFailureCode.INCOMPATIBLE_LINEAGE,
                f"criterion result {result.criterion_id} is not in the applied policy",
            )
        expected = _criterion_outcome(definition, result.observed_value)
        if expected != result.outcome:
            raise PromotionDecisionError(
                PromotionFailureCode.INTEGRITY_FAILURE,
                f"criterion result {result.criterion_id} contradicts its definition",
            )
        if require_pass and result.outcome != CriterionOutcome.PASSED:
            code = (
                PromotionFailureCode.CALIBRATION_METRIC_FAILED
                if result.phase == CriterionPhase.CALIBRATION
                else PromotionFailureCode.VALIDATION_METRIC_FAILED
            )
            raise PromotionDecisionError(code, f"criterion {result.criterion_id} failed")


def _criterion_outcome(
    definition: PromotionCriterionDefinition,
    observed: float | bool | str | None,
) -> CriterionOutcome:
    if observed is None:
        return CriterionOutcome.UNAVAILABLE
    if definition.comparison_operator is None:
        return CriterionOutcome.PASSED if observed is True else CriterionOutcome.FAILED
    threshold = definition.threshold
    if isinstance(observed, (str, bool)) or not isinstance(observed, (int, float)):
        return CriterionOutcome.UNAVAILABLE
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return CriterionOutcome.UNAVAILABLE
    operation = {
        "lt": observed < threshold,
        "lte": observed <= threshold,
        "gt": observed > threshold,
        "gte": observed >= threshold,
        "eq": observed == threshold,
    }[definition.comparison_operator]
    return CriterionOutcome.PASSED if operation else CriterionOutcome.FAILED


def _materialize_decision(
    *,
    output_directory: Path,
    candidate: PromotionCandidateIdentity,
    evidence: PromotionEvidenceLineage,
    criteria_policy: PromotionCriteriaPolicy,
    transition: PromotionTransition,
    criterion_results: tuple[PromotionCriterionResult, ...],
    failure_reasons: tuple[PromotionFailureReason, ...],
    previous_decision_digest: str | None,
    decision_author: str,
    generated_at: datetime | None,
) -> tuple[PromotionDecisionV1, PromotionManifestV1]:
    generated_at = generated_at or datetime.now(UTC)
    decision_payload: dict[str, Any] = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "artifact_type": "commute_help_calibration_promotion_decision",
        "artifact_status": "complete",
        "producer_name": PROMOTION_PRODUCER_NAME,
        "producer_version": PROMOTION_PRODUCER_VERSION,
        "algorithm": PROMOTION_ALGORITHM,
        "decision_author": decision_author,
        "generated_at": generated_at,
        "candidate": candidate.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
        "criteria_policy": criteria_policy.model_dump(mode="json"),
        "transition": transition.model_dump(mode="json"),
        "criterion_results": [item.model_dump(mode="json") for item in criterion_results],
        "failure_reasons": [item.model_dump(mode="json") for item in failure_reasons],
        "previous_decision_digest": previous_decision_digest,
    }
    decision_payload["content_digest"] = _decision_content_digest(decision_payload)
    requested = PromotionDecisionV1.model_validate(decision_payload)
    output_directory = output_directory.resolve()
    if output_directory.exists():
        existing = load_promotion_artifact(output_directory)
        if existing[0].content_digest == requested.content_digest:
            return existing
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            "immutable promotion decision output already exists with another identity",
        )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        decision_path = pending / PROMOTION_DECISION_FILE
        decision_path.write_text(canonical_json(decision_payload) + "\n", encoding="utf-8")
        manifest_payload: dict[str, Any] = {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "artifact_type": "commute_help_calibration_promotion_manifest",
            "artifact_status": "complete",
            "producer_name": PROMOTION_PRODUCER_NAME,
            "producer_version": PROMOTION_PRODUCER_VERSION,
            "generated_at": generated_at,
            "candidate_content_digest": candidate.artifact.content_digest,
            "lifecycle_state": transition.to_state,
            "decision_content_digest": requested.content_digest,
            "previous_decision_digest": previous_decision_digest,
            "decision_output": {
                "relative_path": PROMOTION_DECISION_FILE,
                "sha256": _sha256(decision_path),
                "byte_count": decision_path.stat().st_size,
            },
        }
        manifest_payload["content_digest"] = _manifest_content_digest(manifest_payload)
        PromotionManifestV1.model_validate(manifest_payload)
        (pending / PROMOTION_MANIFEST_FILE).write_text(
            canonical_json(manifest_payload) + "\n", encoding="utf-8"
        )
        os.replace(pending, output_directory)
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise
    return load_promotion_artifact(output_directory)


def _decision_content_digest(payload: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_digest"}
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _manifest_content_digest(payload: dict[str, Any]) -> str:
    semantic = {
        "schema_version": payload["schema_version"],
        "artifact_type": payload["artifact_type"],
        "artifact_status": payload["artifact_status"],
        "producer_name": payload["producer_name"],
        "producer_version": payload["producer_version"],
        "candidate_content_digest": payload["candidate_content_digest"],
        "lifecycle_state": payload["lifecycle_state"],
        "decision_content_digest": payload["decision_content_digest"],
        "previous_decision_digest": payload.get("previous_decision_digest"),
        "decision_relative_path": payload["decision_output"]["relative_path"],
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PromotionDecisionError(
            PromotionFailureCode.INTEGRITY_FAILURE,
            f"expected a JSON object: {path.name}",
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
