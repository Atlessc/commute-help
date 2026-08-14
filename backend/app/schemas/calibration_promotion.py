"""Typed Phase 2.3 calibration/world promotion lifecycle contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROMOTION_SCHEMA_VERSION = 1
PROMOTION_PRODUCER_NAME = "calibration_promotion_gate"
PROMOTION_PRODUCER_VERSION = "phase-2.3-v1"
PROMOTION_ALGORITHM = "explicit-lineage-state-machine-v1"

_SHA256 = r"^[0-9a-f]{64}$"


class PromotionLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    CALIBRATION_PASSED = "calibration_passed"
    VALIDATION_PASSED = "validation_passed"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class PromotionDecisionKind(StrEnum):
    CANDIDATE_REGISTRATION = "candidate_registration"
    CALIBRATION_EVALUATION = "calibration_evaluation"
    VALIDATION_EVALUATION = "validation_evaluation"
    PROMOTION_AUTHORIZATION = "promotion_authorization"
    REJECTION = "rejection"


class PromotionFailureCode(StrEnum):
    CALIBRATION_METRIC_FAILED = "calibration_metric_failed"
    VALIDATION_METRIC_FAILED = "validation_metric_failed"
    MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"
    INCOMPATIBLE_LINEAGE = "incompatible_lineage"
    INCOMPLETE_MEASUREMENT = "incomplete_measurement"
    INTEGRITY_FAILURE = "integrity_failure"
    UNSUPPORTED_COMPARATOR = "unsupported_comparator"
    ILLEGAL_STATE_TRANSITION = "illegal_state_transition"
    PRODUCTION_CRITERIA_UNRESOLVED = "production_criteria_unresolved"


class PromotionCandidateKind(StrEnum):
    CALIBRATION_ARTIFACT = "calibration_artifact"
    REGIONAL_WORLD_CANDIDATE = "regional_world_candidate"
    SYNTHETIC_FIXTURE = "synthetic_fixture"


class CriteriaPolicyStatus(StrEnum):
    FIXTURE_LOCAL = "fixture_local"
    UNRESOLVED_PRODUCTION = "unresolved_production"
    APPROVED_PRODUCTION = "approved_production"


class CriterionPhase(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"


class CriterionOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class StrictPromotionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PromotionArtifactIdentity(StrictPromotionModel):
    artifact_type: str = Field(min_length=1, max_length=120)
    artifact_id: str = Field(min_length=1, max_length=240)
    content_digest: str = Field(pattern=_SHA256)
    producer_name: str = Field(min_length=1, max_length=120)
    producer_version: str = Field(min_length=1, max_length=120)


class PromotionCandidateIdentity(StrictPromotionModel):
    candidate_kind: PromotionCandidateKind
    artifact: PromotionArtifactIdentity
    parent_lineage: tuple[PromotionArtifactIdentity, ...] = ()

    @model_validator(mode="after")
    def validate_candidate(self) -> PromotionCandidateIdentity:
        digests = [item.content_digest for item in self.parent_lineage]
        if len(digests) != len(set(digests)):
            raise ValueError("candidate parent lineage must be unique")
        return self


class ComparatorMeasurementLineage(StrictPromotionModel):
    comparator_name: str = Field(min_length=1, max_length=120)
    comparator_version: str = Field(min_length=1, max_length=120)
    comparator_algorithm: str = Field(min_length=1, max_length=120)
    content_digest: str = Field(pattern=_SHA256)
    measurement_semantics: str = Field(min_length=1, max_length=240)
    evidence_level: str = Field(min_length=1, max_length=80)
    calibration_status: str = Field(min_length=1, max_length=80)


class PromotionEvidenceLineage(StrictPromotionModel):
    comparator: ComparatorMeasurementLineage
    sources: tuple[PromotionArtifactIdentity, ...]
    integrity_check_passed: bool
    measurement_complete: bool

    @model_validator(mode="after")
    def validate_sources(self) -> PromotionEvidenceLineage:
        if not self.sources:
            raise ValueError("promotion evidence requires immutable source lineage")
        digests = [item.content_digest for item in self.sources]
        if len(digests) != len(set(digests)):
            raise ValueError("promotion evidence source lineage must be unique")
        return self


class PromotionCriterionDefinition(StrictPromotionModel):
    criterion_id: str = Field(min_length=1, max_length=160)
    phase: CriterionPhase
    description: str = Field(min_length=1, max_length=500)
    metric_name: str | None = Field(default=None, min_length=1, max_length=160)
    comparison_operator: Literal["lt", "lte", "gt", "gte", "eq"] | None = None
    threshold: float | int | bool | None = None

    @model_validator(mode="after")
    def validate_threshold(self) -> PromotionCriterionDefinition:
        has_operator = self.comparison_operator is not None
        has_threshold = self.threshold is not None
        if has_operator != has_threshold:
            raise ValueError("criterion operator and threshold must appear together")
        return self


class PromotionCriteriaPolicy(StrictPromotionModel):
    policy_name: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=120)
    policy_status: CriteriaPolicyStatus
    criteria: tuple[PromotionCriterionDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> PromotionCriteriaPolicy:
        identifiers = [item.criterion_id for item in self.criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("promotion criterion IDs must be unique")
        if self.policy_status == CriteriaPolicyStatus.UNRESOLVED_PRODUCTION:
            if self.criteria:
                raise ValueError("unresolved production policy cannot contain thresholds")
        elif not self.criteria:
            raise ValueError("resolved criteria policy must contain criteria")
        elif {item.phase for item in self.criteria} != {
            CriterionPhase.CALIBRATION,
            CriterionPhase.VALIDATION,
        }:
            raise ValueError(
                "resolved criteria policy requires calibration and validation criteria"
            )
        if self.policy_status == CriteriaPolicyStatus.FIXTURE_LOCAL and any(
            not item.criterion_id.startswith("synthetic.") for item in self.criteria
        ):
            raise ValueError("fixture-local criterion IDs must start with synthetic.")
        return self


class PromotionCriterionResult(StrictPromotionModel):
    criterion_id: str = Field(min_length=1, max_length=160)
    phase: CriterionPhase
    outcome: CriterionOutcome
    observed_value: float | int | bool | str | None = None
    detail: str = Field(min_length=1, max_length=1000)


class PromotionFailureReason(StrictPromotionModel):
    code: PromotionFailureCode
    detail: str = Field(min_length=1, max_length=1000)
    criterion_id: str | None = Field(default=None, min_length=1, max_length=160)


class PromotionTransition(StrictPromotionModel):
    from_state: PromotionLifecycleState | None
    to_state: PromotionLifecycleState
    decision_kind: PromotionDecisionKind

    @model_validator(mode="after")
    def validate_shape(self) -> PromotionTransition:
        expected = {
            (None, PromotionLifecycleState.CANDIDATE): (
                PromotionDecisionKind.CANDIDATE_REGISTRATION
            ),
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
        if expected.get((self.from_state, self.to_state)) != self.decision_kind:
            raise ValueError("illegal promotion lifecycle transition")
        return self


class PromotionDecisionV1(StrictPromotionModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_calibration_promotion_decision"]
    artifact_status: Literal["complete"]
    producer_name: Literal["calibration_promotion_gate"]
    producer_version: Literal["phase-2.3-v1"]
    algorithm: Literal["explicit-lineage-state-machine-v1"]
    decision_author: str = Field(min_length=1, max_length=160)
    generated_at: datetime
    candidate: PromotionCandidateIdentity
    evidence: PromotionEvidenceLineage
    criteria_policy: PromotionCriteriaPolicy
    transition: PromotionTransition
    criterion_results: tuple[PromotionCriterionResult, ...] = ()
    failure_reasons: tuple[PromotionFailureReason, ...] = ()
    previous_decision_digest: str | None = Field(default=None, pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_decision(self) -> PromotionDecisionV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.transition.from_state is None and self.previous_decision_digest:
            raise ValueError("candidate registration cannot have a previous decision")
        if self.transition.from_state is not None and not self.previous_decision_digest:
            raise ValueError("lifecycle transition requires previous decision lineage")
        result_ids = [item.criterion_id for item in self.criterion_results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("criterion results must be unique")
        if self.transition.to_state == PromotionLifecycleState.REJECTED:
            if not self.failure_reasons:
                raise ValueError("rejected decision requires explicit failure reasons")
        elif self.failure_reasons:
            raise ValueError("non-rejected decision cannot contain failure reasons")
        if (
            self.criteria_policy.policy_status == CriteriaPolicyStatus.FIXTURE_LOCAL
            and self.candidate.candidate_kind
            != PromotionCandidateKind.SYNTHETIC_FIXTURE
        ):
            raise ValueError("fixture-local criteria may promote only synthetic fixtures")
        return self


class PromotionDecisionFileIdentity(StrictPromotionModel):
    relative_path: Literal["promotion-decision.json"]
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)


class PromotionManifestV1(StrictPromotionModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_calibration_promotion_manifest"]
    artifact_status: Literal["complete"]
    producer_name: Literal["calibration_promotion_gate"]
    producer_version: Literal["phase-2.3-v1"]
    generated_at: datetime
    candidate_content_digest: str = Field(pattern=_SHA256)
    lifecycle_state: PromotionLifecycleState
    decision_content_digest: str = Field(pattern=_SHA256)
    previous_decision_digest: str | None = Field(default=None, pattern=_SHA256)
    decision_output: PromotionDecisionFileIdentity
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> PromotionManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return self
