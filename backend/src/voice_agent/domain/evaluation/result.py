"""One content-immutable execution attempt for a run/case/repetition slot (docs/16 §8).

While ``pending``/``running`` the runner fills execution fields through
reserve/start/finalize. After the attempt is terminal only ``status`` (to
``invalid``), ``validity``, ``is_current_attempt``, ``human_review_summary``,
``expires_at``, ``updated_at`` and ``result_revision`` may change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    PreciseDecimal,
    ShortLabel,
    UtcDatetime,
)
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.common import (
    EVALUATION_SCHEMA_VERSION,
    MAX_ASSERTIONS,
    MAX_EVAL_INPUT_CHARS,
    MAX_EVAL_REFERENCE_CHARS,
    MAX_EVIDENCE_REFERENCES,
    EvalText,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationSeverity,
    EvaluationSplit,
    SafeActorRef,
    SafeCode,
)
from voice_agent.domain.records_common import (
    Checksum,
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    Revision,
)

Ids = Annotated[tuple[CanonicalId, ...], Field(max_length=MAX_EVIDENCE_REFERENCES)]
OutputText = Annotated[str, Field(min_length=1, max_length=MAX_EVAL_REFERENCE_CHARS)]
Ms = Annotated[PreciseDecimal, Field(ge=0)]


class ResultStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALID = "invalid"


TERMINAL_RESULT_STATES: frozenset[ResultStatus] = frozenset(
    {ResultStatus.COMPLETED, ResultStatus.FAILED, ResultStatus.CANCELLED, ResultStatus.INVALID}
)
FINAL_RESULT_STATES: frozenset[ResultStatus] = frozenset(
    {ResultStatus.COMPLETED, ResultStatus.FAILED, ResultStatus.CANCELLED}
)


class InvalidationSource(StrEnum):
    HARNESS = "harness"
    TEST_SETUP = "test_setup"
    APPLICATION = "application"
    PROVIDER = "provider"


RERUN_SOURCES: frozenset[InvalidationSource] = frozenset(
    {InvalidationSource.HARNESS, InvalidationSource.TEST_SETUP}
)


class Validity(RecordModel):
    is_valid_sample: bool
    reason_code: SafeCode | None = None
    note: EvalText | None = None
    invalidation_source: InvalidationSource | None = None
    counts_in_quality: bool = True
    counts_in_reliability: bool = True
    counts_in_latency: bool = True
    counts_in_cost: bool = True
    invalidated_at: UtcDatetime | None = None
    invalidated_by: SafeActorRef | None = None

    @model_validator(mode="after")
    def _invalid_needs_evidence(self) -> Validity:
        if not self.is_valid_sample and (
            self.reason_code is None or self.invalidation_source is None
        ):
            raise ValueError("an invalid sample retains its reason and source")
        return self


class EvidenceReferences(RecordModel):
    session_id: CanonicalId | None = None
    turn_ids: Ids = ()
    operation_ids: Ids = ()
    cost_entry_ids: Ids = ()
    calculation_run_id: CanonicalId | None = None
    event_ids: Ids = ()
    local_report_reference: ExternalIdentifier | None = None
    local_report_checksum: Checksum | None = None


class OutputEvidence(RecordModel):
    mode: Literal["embedded_safe_text", "session_references"]
    accepted_final_transcript: (
        Annotated[str, Field(min_length=1, max_length=MAX_EVAL_INPUT_CHARS)] | None
    ) = None
    generated_response: OutputText | None = None
    tts_submitted_response: OutputText | None = None
    delivered_status: SafeCode | None = None
    response_language: SafeCode | None = None
    response_script: SafeCode | None = None
    terminal_state: SafeCode | None = None
    greeting_count: NonNegativeInt | None = None
    fallback_code: SafeCode | None = None


class AssertionResult(RecordModel):
    assertion_id: SafeCode
    assertion_type: SafeCode
    rule_version: ShortLabel
    outcome: Literal["passed", "failed", "not_applicable", "unavailable"]
    severity: EvaluationSeverity
    critical: bool
    actual_summary: EvalText | None = None
    expected_summary: EvalText | None = None
    reason_code: SafeCode | None = None
    evaluated_at: UtcDatetime


class ResultMeasurements(RecordModel):
    metric_schema_version: ShortLabel
    language_correct: bool | None = None
    script_correct: bool | None = None
    instruction_followed: bool | None = None
    format_correct: bool | None = None
    transcript_correct: bool | None = None
    terms_correct: bool | None = None
    end_to_end_success: bool | None = None
    lifecycle_correct: bool | None = None
    speech_end_to_playback_ms: Ms | None = None
    stt_final_ms: Ms | None = None
    llm_first_token_ms: Ms | None = None
    llm_completion_ms: Ms | None = None
    tts_first_audio_ms: Ms | None = None
    tts_completion_ms: Ms | None = None
    publish_to_playback_ms: Ms | None = None
    interruption_to_silence_ms: Ms | None = None
    reconnect_ms: Ms | None = None
    completion_ms: Ms | None = None
    retry_count: NonNegativeInt | None = None
    failure_count: NonNegativeInt | None = None
    terminal_outcome: SafeCode | None = None


class UsageAndCost(RecordModel):
    rate_card_id: ShortLabel
    calculation_run_id: CanonicalId | None = None
    operation_ids: Ids = ()
    cost_entry_ids: Ids = ()
    gross_cost_usd: PreciseDecimal | None = None
    net_cost_usd: PreciseDecimal | None = None
    marginal_cost_inr: PreciseDecimal | None = None
    allocated_cost_inr: PreciseDecimal | None = None
    usage_status: Literal["provider_reported", "measured", "estimated", "unavailable"]
    calculation_status: Literal["pending", "partial", "final", "failed", "unavailable"]
    reconciliation_status: Literal["not_checked", "matched", "mismatch", "unavailable"]


class DimensionAggregate(RecordModel):
    count: PositiveInt
    mean: Annotated[Decimal, Field(ge=1, le=5)]


class HumanReviewSummary(RecordModel):
    review_status: Literal["not_required", "pending", "in_progress", "complete"]
    expected_reviewer_count: NonNegativeInt
    submitted_reviewer_count: NonNegativeInt = 0
    dimension_aggregates: dict[str, DimensionAggregate] | None = None
    overall_mean: Annotated[Decimal, Field(ge=1, le=5)] | None = None
    low_score_count: NonNegativeInt = 0
    reason_code_counts: dict[str, NonNegativeInt] | None = None
    aggregated_at: UtcDatetime | None = None
    aggregation_version: ShortLabel

    @model_validator(mode="after")
    def _complete_needs_ratings(self) -> HumanReviewSummary:
        complete = self.review_status == "complete"
        if complete and self.submitted_reviewer_count < self.expected_reviewer_count:
            raise ValueError("complete review requires every expected current rating")
        return self


class ResultFailure(RecordModel):
    failure_source: Literal["application", "provider", "harness", "test_setup"]
    error_type: SafeCode | None = None
    message_safe: EvalText


class EvaluationResult(RecordModel):
    evaluation_result_id: CanonicalId
    evaluation_run_id: CanonicalId
    evaluation_dataset_id: CanonicalId
    evaluation_case_id: CanonicalId
    case_key: SafeCode
    case_sequence_number: PositiveInt
    layer: EvaluationLayer
    category: SafeCode
    severity: EvaluationSeverity
    split: EvaluationSplit
    repetition_index: Annotated[int, Field(strict=True, ge=1, le=3)]
    attempt_index: PositiveInt
    is_current_attempt: bool
    supersedes_evaluation_result_id: CanonicalId | None = None
    status: ResultStatus
    result_revision: Revision
    validity: Validity
    evidence_references: EvidenceReferences
    output_evidence: OutputEvidence
    assertion_results: Annotated[tuple[AssertionResult, ...], Field(max_length=MAX_ASSERTIONS)] = ()
    critical_failures: Annotated[tuple[SafeCode, ...], Field(max_length=MAX_ASSERTIONS)] = ()
    measurements: ResultMeasurements
    usage_and_cost: UsageAndCost
    human_review_summary: HumanReviewSummary
    failure: ResultFailure | None = None
    schema_version: Literal[1] = EVALUATION_SCHEMA_VERSION
    environment: EvaluationEnvironment
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    updated_at: UtcDatetime
    expires_at: UtcDatetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RESULT_STATES

    @property
    def slot(self) -> tuple[str, str, int]:
        return (self.evaluation_run_id, self.evaluation_case_id, self.repetition_index)

    @model_validator(mode="after")
    def _lifecycle(self) -> EvaluationResult:
        if self.is_terminal and self.ended_at is None:
            raise ValueError("a terminal result requires ended_at")
        never_started = {ResultStatus.PENDING, ResultStatus.CANCELLED, ResultStatus.INVALID}
        if self.status not in never_started and self.started_at is None:
            raise ValueError("a started result requires started_at")
        if (self.attempt_index > 1) != (self.supersedes_evaluation_result_id is not None):
            raise ValueError("a rerun attempt references exactly the attempt it supersedes")
        if self.status is ResultStatus.INVALID and self.validity.is_valid_sample:
            raise ValueError("an invalid result carries invalid-sample evidence")
        return self

    def start(self, *, now: datetime) -> EvaluationResult:
        if self.status is not ResultStatus.PENDING:
            raise DomainRuleError("only a pending attempt can start")
        return self._revise(status=ResultStatus.RUNNING, started_at=now, updated_at=now)

    def finalize(
        self, *, status: ResultStatus, now: datetime, **evidence: object
    ) -> EvaluationResult:
        if status not in FINAL_RESULT_STATES:
            raise DomainRuleError("finalize records completed, failed, or cancelled")
        if self.is_terminal:
            raise DomainRuleError("terminal execution evidence is immutable")
        allowed = {
            "evidence_references",
            "output_evidence",
            "assertion_results",
            "critical_failures",
            "measurements",
            "usage_and_cost",
            "failure",
            "human_review_summary",
        }
        if not set(evidence) <= allowed:
            raise DomainRuleError("finalize accepts execution evidence fields only")
        return self._revise(status=status, ended_at=now, updated_at=now, **evidence)

    def mark_invalid(self, validity: Validity, *, now: datetime) -> EvaluationResult:
        if validity.is_valid_sample:
            raise DomainRuleError("mark-invalid needs invalid-sample evidence")
        ended = self.ended_at or now
        return self._revise(
            status=ResultStatus.INVALID, validity=validity, ended_at=ended, updated_at=now
        )

    def superseded(self, *, now: datetime) -> EvaluationResult:
        return self._revise(is_current_attempt=False, updated_at=now)

    def _revise(self, **changes: object) -> EvaluationResult:
        data = {**self.model_dump(), **changes, "result_revision": self.result_revision + 1}
        return EvaluationResult.model_validate(data)
