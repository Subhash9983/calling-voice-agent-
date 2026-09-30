"""Provider operation (one attempt) lifecycle (docs/01 §16, docs/02 §8).

Every retry is a new operation with a new ``operation_id`` sharing the
logical request and turn IDs. A cancelled operation's late result is
``discarded_late`` and cannot affect user-visible output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import Field, JsonValue, field_validator

from voice_agent.contracts.base import CanonicalId, ShortLabel, StrictModel, UtcDatetime
from voice_agent.contracts.enums import OperationComponent, OperationStatus, ResultDisposition
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.usage import UsageReport
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError

MAX_RESULT_SUMMARY_KEYS = 50
MAX_RESULT_SUMMARY_BYTES = 8 * 1024
DurationMs = Annotated[int, Field(ge=0)]
_O = OperationStatus
_TERMINAL = frozenset({_O.SUCCEEDED, _O.FAILED, _O.TIMED_OUT, _O.CANCELLED})
# docs/02 §8 lists the states and terminal set; the non-terminal ordering
# below (created -> queued -> started -> streaming) is the implied lifecycle.
OPERATION_TRANSITIONS: Mapping[OperationStatus, frozenset[OperationStatus]] = MappingProxyType(
    {
        _O.CREATED: frozenset({_O.QUEUED, _O.STARTED, _O.FAILED, _O.CANCELLED}),
        _O.QUEUED: frozenset({_O.STARTED, _O.FAILED, _O.TIMED_OUT, _O.CANCELLED}),
        _O.STARTED: frozenset({_O.STREAMING, *_TERMINAL}),
        _O.STREAMING: _TERMINAL,
        _O.SUCCEEDED: frozenset(),
        _O.FAILED: frozenset(),
        _O.TIMED_OUT: frozenset(),
        _O.CANCELLED: frozenset(),
    }
)


class ProviderOperation(StrictModel):
    operation_id: CanonicalId
    logical_request_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    component: OperationComponent
    operation_type: ShortLabel
    provider: ShortLabel
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    attempt_number: Annotated[int, Field(ge=1)] = 1
    previous_attempt_operation_id: CanonicalId | None = None
    worker_generation: Annotated[int, Field(ge=1)]
    status: OperationStatus = OperationStatus.CREATED
    status_revision: Annotated[int, Field(ge=0)] = 0
    result_disposition: ResultDisposition = ResultDisposition.NOT_APPLICABLE
    usage: UsageReport = UsageReport.unavailable()
    failure: NormalizedFailure | None = None
    cancellation_requested: bool = False
    # Timing/result evidence (docs/02 §8); unavailable values stay ``None``.
    started_at: UtcDatetime | None = None
    first_result_at: UtcDatetime | None = None
    time_to_first_result_ms: DurationMs | None = None
    provider_duration_ms: DurationMs | None = None
    total_duration_ms: DurationMs | None = None
    result_summary: dict[str, JsonValue] | None = None

    @field_validator("result_summary")
    @classmethod
    def _bounded_summary(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        if len(value) > MAX_RESULT_SUMMARY_KEYS:
            raise ValueError("result_summary allows at most 50 keys")
        if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > MAX_RESULT_SUMMARY_BYTES:
            raise ValueError("result_summary exceeds 8 KiB")
        return value

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def transition_to(self, target: OperationStatus, **update: object) -> ProviderOperation:
        if target not in OPERATION_TRANSITIONS[self.status]:
            raise InvalidTransitionError("operation", self.status.value, target.value)
        return self.model_copy(
            update={"status": target, "status_revision": self.status_revision + 1, **update}
        )

    def succeed(self, usage: UsageReport) -> ProviderOperation:
        return self.transition_to(
            OperationStatus.SUCCEEDED, usage=usage, result_disposition=ResultDisposition.USED
        )

    def fail(self, failure: NormalizedFailure, usage: UsageReport) -> ProviderOperation:
        return self.transition_to(OperationStatus.FAILED, failure=failure, usage=usage)

    def cancel(self, usage: UsageReport | None = None) -> ProviderOperation:
        return self.transition_to(
            OperationStatus.CANCELLED,
            usage=usage if usage is not None else self.usage,
            cancellation_requested=True,
            result_disposition=ResultDisposition.DISCARDED_LATE,
        )

    def with_late_usage(self, usage: UsageReport) -> ProviderOperation:
        """Late usage may update cost after the audible turn (docs/01 §10)."""
        if not self.is_terminal:
            raise DomainRuleError("late usage applies only to terminal operations")
        return self.model_copy(update={"usage": usage})

    def next_attempt(self, operation_id: str) -> ProviderOperation:
        """Create the retry attempt: new operation ID, same logical request and turn."""
        if operation_id == self.operation_id:
            raise DomainRuleError("a retry requires a new operation ID")
        return ProviderOperation(
            operation_id=operation_id,
            logical_request_id=self.logical_request_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            component=self.component,
            operation_type=self.operation_type,
            provider=self.provider,
            model=self.model,
            attempt_number=self.attempt_number + 1,
            previous_attempt_operation_id=self.operation_id,
            worker_generation=self.worker_generation,
        )
