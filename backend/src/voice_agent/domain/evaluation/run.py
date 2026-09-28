"""Evaluation run: immutable configuration plus a revision-checked lifecycle (docs/16 §7).

After the run leaves ``queued`` its dataset, configuration, execution
policy, gates, and rate card cannot change. Terminal statuses set
``ended_at`` (the retention anchor); a nonterminal run whose status has not
changed for 30 days becomes ``abandoned``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    PreciseDecimal,
    ShortLabel,
    UtcDatetime,
)
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError
from voice_agent.domain.evaluation.common import (
    EVALUATION_SCHEMA_VERSION,
    MAX_EVIDENCE_REFERENCES,
    EvalText,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
    SafeActorRef,
    SafeCode,
)
from voice_agent.domain.evaluation.dataset import LayerCounts, LayerRepetitionCounts, SplitCounts
from voice_agent.domain.records_common import (
    MAX_NAME_CHARS,
    Checksum,
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    Revision,
    bounded_container,
)
from voice_agent.privacy_and_retention.expiry import evaluation_run_expires_at


class RunStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALID = "invalid"
    ABANDONED = "abandoned"


_R = RunStatus
TERMINAL_RUN_STATES: frozenset[RunStatus] = frozenset(
    {_R.COMPLETED, _R.FAILED, _R.CANCELLED, _R.INVALID, _R.ABANDONED}
)
NONTERMINAL_RUN_STATES: frozenset[RunStatus] = frozenset(set(RunStatus) - TERMINAL_RUN_STATES)
_STOPS = frozenset({_R.FAILED, _R.CANCELLED, _R.INVALID, _R.ABANDONED})
# The status list is approved (docs/16 §7); this forward-only order is the
# implied lifecycle. Every nonterminal state may stop terminally.
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType(
    {
        _R.QUEUED: frozenset({_R.VALIDATING, *_STOPS}),
        _R.VALIDATING: frozenset({_R.RUNNING, *_STOPS}),
        _R.RUNNING: frozenset({_R.AWAITING_HUMAN_REVIEW, _R.RECONCILING, _R.COMPLETED, *_STOPS}),
        _R.AWAITING_HUMAN_REVIEW: frozenset({_R.RECONCILING, _R.COMPLETED, *_STOPS}),
        _R.RECONCILING: frozenset({_R.AWAITING_HUMAN_REVIEW, _R.COMPLETED, *_STOPS}),
        **{state: frozenset() for state in TERMINAL_RUN_STATES},
    }
)
SafeSummary = Annotated[dict[str, JsonValue], bounded_container(64 * 1024, 50)]
SafeOptions = Annotated[dict[str, JsonValue], bounded_container(16 * 1024, 50)]


class DatasetSnapshot(RecordModel):
    evaluation_dataset_id: CanonicalId
    dataset_key: ShortLabel
    version: PositiveInt
    case_set_checksum: Checksum
    total_case_count: NonNegativeInt
    layer_counts: LayerCounts
    split_counts: SplitCounts
    selected_splits: Annotated[tuple[EvaluationSplit, ...], Field(min_length=1, max_length=2)]
    case_subset_checksum: Checksum | None = None
    holdout_access: bool


class ComponentIdentity(RecordModel):
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    adapter_version: ExternalIdentifier


class ConfigurationSnapshot(RecordModel):
    agent_config_id: CanonicalId
    agent_config_version: PositiveInt
    config_checksum: Checksum
    prompt_id: ExternalIdentifier
    prompt_version: ShortLabel
    prompt_checksum: Checksum
    transport: ComponentIdentity
    stt: ComponentIdentity
    conversation_engine: ComponentIdentity
    tts: ComponentIdentity
    safe_runtime_options: SafeOptions | None = None
    application_version: ShortLabel
    commit_reference: ShortLabel
    python_lock_checksum: Checksum
    frontend_lock_checksum: Checksum
    runner_version: ShortLabel
    rule_version: ShortLabel
    rubric_version: ShortLabel
    rate_card_id: ShortLabel
    environment_label: ShortLabel
    region_label: ShortLabel | None = None


class ExecutionPolicy(RecordModel):
    selected_layers: Annotated[tuple[EvaluationLayer, ...], Field(min_length=1, max_length=3)]
    selected_splits: Annotated[tuple[EvaluationSplit, ...], Field(min_length=1, max_length=2)]
    selected_case_ids: Annotated[tuple[CanonicalId, ...], Field(max_length=100)] = ()
    layer_repetitions: LayerRepetitionCounts = LayerRepetitionCounts()
    expected_slot_count: NonNegativeInt
    requested_concurrency: PositiveInt
    actual_concurrency: PositiveInt
    runner_retry_limit: Annotated[int, Field(strict=True, ge=0, le=3)]
    random_seed: NonNegativeInt | None = None
    order_policy: SafeCode
    live_case_manual_execution: bool
    invalid_sample_policy: SafeCode
    stop_on_critical: bool
    spend_cap_inr: PreciseDecimal | None = None


class GateDefinition(RecordModel):
    gate_id: SafeCode
    metric: SafeCode
    comparator: Literal["gte", "lte", "eq"]
    threshold: PreciseDecimal
    critical: bool


class GateSnapshot(RecordModel):
    gate_set_version: ShortLabel
    gates: Annotated[tuple[GateDefinition, ...], Field(max_length=MAX_EVIDENCE_REFERENCES)]


class RunProgress(RecordModel):
    expected: NonNegativeInt
    completed: NonNegativeInt = 0
    failed: NonNegativeInt = 0
    invalid: NonNegativeInt = 0
    cancelled: NonNegativeInt = 0
    pending: NonNegativeInt = 0


class RunFailure(RecordModel):
    failure_code: SafeCode
    message_safe: EvalText
    occurred_at: UtcDatetime


class EvaluationRun(RecordModel):
    evaluation_run_id: CanonicalId
    client_request_id: CanonicalId
    benchmark_group_id: CanonicalId | None = None
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_CHARS)]
    purpose: EvaluationPurpose
    status: RunStatus
    status_revision: Revision
    dataset_snapshot: DatasetSnapshot
    configuration_snapshot: ConfigurationSnapshot
    execution_policy: ExecutionPolicy
    gate_snapshot: GateSnapshot
    progress: RunProgress
    summary: SafeSummary | None = None
    failure: RunFailure | None = None
    schema_version: Literal[1] = EVALUATION_SCHEMA_VERSION
    environment: EvaluationEnvironment
    initiated_by: SafeActorRef
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    updated_at: UtcDatetime
    status_changed_at: UtcDatetime
    expires_at: UtcDatetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATES

    @model_validator(mode="after")
    def _lifecycle(self) -> EvaluationRun:
        if self.is_terminal and self.ended_at is None:
            raise ValueError("a terminal run requires ended_at")
        if not self.is_terminal and (self.ended_at is not None or self.expires_at is not None):
            raise ValueError("a nonterminal run has no ended_at or expires_at")
        started = self.status not in {RunStatus.QUEUED, RunStatus.VALIDATING}
        if started and not self.is_terminal and self.started_at is None:
            raise ValueError("a started run requires started_at")
        return self

    def transition(
        self, target: RunStatus, *, now: datetime, failure: RunFailure | None = None
    ) -> EvaluationRun:
        if target not in RUN_TRANSITIONS[self.status]:
            raise InvalidTransitionError("evaluation_run", self.status.value, target.value)
        update: dict[str, object] = {
            "status": target,
            "status_revision": self.status_revision + 1,
            "status_changed_at": now,
            "updated_at": now,
        }
        if target is RunStatus.RUNNING and self.started_at is None:
            update["started_at"] = now
        if target in TERMINAL_RUN_STATES:
            update["ended_at"] = now
        if failure is not None:
            update["failure"] = failure
        return self.model_copy(update=update)

    def with_progress(self, progress: RunProgress, *, now: datetime) -> EvaluationRun:
        if self.is_terminal:
            raise DomainRuleError("a terminal run's progress is final")
        return self.model_copy(
            update={
                "progress": progress,
                "status_revision": self.status_revision + 1,
                "updated_at": now,
            }
        )

    def with_summary(self, summary: dict[str, JsonValue], *, now: datetime) -> EvaluationRun:
        data = {
            **self.model_dump(),
            "summary": summary,
            "updated_at": now,
            "status_revision": self.status_revision + 1,
        }
        return EvaluationRun.model_validate(data)

    def with_expiry(self) -> EvaluationRun:
        if self.ended_at is None:
            raise DomainRuleError("only a terminal run can be marked for expiry")
        return self.model_copy(update={"expires_at": evaluation_run_expires_at(self.ended_at)})
