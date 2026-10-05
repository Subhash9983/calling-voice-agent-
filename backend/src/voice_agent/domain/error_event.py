"""Normalized searchable failure record (docs/02 §12).

The original classification and diagnostics are immutable; only the
``resolution`` section changes, through a revision-checked operation. The
fingerprint excludes transcripts, prompts, user/session IDs, raw messages,
and secrets.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.contracts.failures import (
    ERROR_CATEGORY_BY_TYPE,
    ERROR_TAXONOMY_VERSION,
    ErrorCategory,
    ErrorComponent,
    ErrorType,
    NormalizedFailure,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.records_common import (
    MAX_BROWSER_MESSAGE_CHARS,
    Checksum,
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    RedactionStatus,
    Revision,
    SafeDetails,
    SafeMessage,
    SafeNote,
)

ERROR_EVENT_SCHEMA_VERSION: Final = 1
_PROVIDER_COMPONENTS = frozenset(
    {
        ErrorComponent.TRANSPORT,
        ErrorComponent.STT,
        ErrorComponent.CONVERSATION_ENGINE,
        ErrorComponent.TTS,
        ErrorComponent.RETRIEVAL,
    }
)


class ErrorSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorOrigin(StrEnum):
    CLIENT = "client"
    APPLICATION = "application"
    ADAPTER = "adapter"
    PROVIDER = "provider"
    INFRASTRUCTURE = "infrastructure"


class RetryBlockedReason(StrEnum):
    NOT_RETRYABLE = "not_retryable"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    SESSION_CANCELLED = "session_cancelled"
    TURN_CANCELLED = "turn_cancelled"
    NON_IDEMPOTENT_ACTION = "non_idempotent_action"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class ResolutionStatus(StrEnum):
    OPEN = "open"
    RETRYING = "retrying"
    RECOVERED = "recovered"
    UNRECOVERABLE = "unrecoverable"
    IGNORED_EXPECTED = "ignored_expected"


class ResolutionAction(StrEnum):
    RETRY_SUCCEEDED = "retry_succeeded"
    FALLBACK_SUCCEEDED = "fallback_succeeded"
    RECONNECTED = "reconnected"
    USER_RETRIED = "user_retried"
    CONFIGURATION_CHANGED = "configuration_changed"
    PROVIDER_RECOVERED = "provider_recovered"
    SESSION_ENDED = "session_ended"
    NONE = "none"


class ErrorRetentionClass(StrEnum):
    OPERATIONAL_ERROR = "operational_error"
    SECURITY_ERROR = "security_error"
    BILLING_ERROR = "billing_error"


class ErrorProviderContext(RecordModel):
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    adapter_version: ExternalIdentifier
    provider_request_id: ExternalIdentifier | None = None
    provider_error_code: ShortLabel | None = None
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None
    websocket_close_code: Annotated[int, Field(strict=True, ge=1000, le=4999)] | None = None
    region_label: ShortLabel | None = None


class ErrorRetry(RecordModel):
    retryable: bool
    retry_scheduled: bool
    retry_attempt_number: PositiveInt | None = None
    retry_after_ms: NonNegativeInt | None = None
    retry_operation_id: CanonicalId | None = None
    retry_blocked_reason: RetryBlockedReason | None = None


class ErrorFallback(RecordModel):
    attempted: bool
    succeeded: bool
    fallback_provider: ShortLabel | None = None
    fallback_model: ExternalIdentifier | None = None
    fallback_operation_id: CanonicalId | None = None
    failure_reason: SafeNote | None = None


class ErrorImpact(RecordModel):
    user_affected: bool
    session_impact: Literal["none", "degraded", "terminated"]
    turn_impact: Literal["none", "delayed", "failed", "interrupted", "abandoned"]
    audio_impact: Literal["none", "delayed", "distorted", "stopped", "unavailable"]
    cost_may_be_incurred: bool


class ErrorResolution(RecordModel):
    status: ResolutionStatus
    revision: Revision
    resolved_at: UtcDatetime | None = None
    resolution_action: ResolutionAction | None = None
    resolved_by_operation_id: CanonicalId | None = None
    resolution_note: SafeNote | None = None


class ErrorStateSnapshot(RecordModel):
    session_state: ShortLabel | None = None
    agent_activity_state: ShortLabel | None = None
    turn_state: ShortLabel | None = None
    operation_state: ShortLabel | None = None
    worker_generation: PositiveInt | None = None
    retry_attempt_number: PositiveInt | None = None


class BrowserErrorMessage(RecordModel):
    code: ShortLabel
    message_safe: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_MESSAGE_CHARS)]
    retry_allowed: bool
    suggested_action: (
        Literal[
            "wait",
            "retry",
            "check_microphone",
            "check_connection",
            "restart_session",
            "contact_support",
        ]
        | None
    ) = None


class ErrorEventRecord(RecordModel):
    error_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    logical_request_id: CanonicalId | None = None
    event_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    root_error_id: CanonicalId | None = None
    caused_by_error_id: CanonicalId | None = None
    error_fingerprint: Checksum
    schema_version: Literal[1] = ERROR_EVENT_SCHEMA_VERSION
    error_taxonomy_version: PositiveInt = ERROR_TAXONOMY_VERSION
    error_type: ErrorType
    category: ErrorCategory
    severity: ErrorSeverity
    component: ErrorComponent
    origin: ErrorOrigin
    failure_phase: ShortLabel | None = None
    provider_context: ErrorProviderContext | None = None
    is_expected: bool
    counts_toward_failure_rate: bool
    retry: ErrorRetry
    fallback: ErrorFallback
    impact: ErrorImpact
    resolution: ErrorResolution
    diagnostic_code: ShortLabel
    message_safe: SafeMessage
    exception_class_safe: ShortLabel | None = None
    safe_details: SafeDetails | None = None
    restricted_log_reference: ExternalIdentifier | None = None
    state_snapshot: ErrorStateSnapshot | None = None
    user_message: BrowserErrorMessage | None = None
    occurred_at: UtcDatetime
    detected_at: UtcDatetime
    recorded_at: UtcDatetime
    updated_at: UtcDatetime
    resolved_at: UtcDatetime | None = None
    environment: AgentConfigEnvironment
    redaction_status: RedactionStatus = RedactionStatus.NOT_REQUIRED
    retention_class: ErrorRetentionClass = ErrorRetentionClass.OPERATIONAL_ERROR
    expires_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _classification(self) -> ErrorEventRecord:
        if ERROR_CATEGORY_BY_TYPE[self.error_type] is not self.category:
            raise ValueError("category must match the error type")
        if self.is_expected and self.counts_toward_failure_rate:
            raise ValueError("an expected error does not count toward the failure rate")
        return self

    def resolve(
        self,
        *,
        status: ResolutionStatus,
        action: ResolutionAction | None,
        now: datetime,
        note: str | None = None,
        resolved_by_operation_id: str | None = None,
    ) -> ErrorEventRecord:
        """Only the resolution section changes (docs/02 §12)."""
        if status is ResolutionStatus.OPEN:
            raise DomainRuleError("a resolution cannot reopen an error")
        terminal = status is not ResolutionStatus.RETRYING
        resolution = ErrorResolution(
            status=status,
            revision=self.resolution.revision + 1,
            resolved_at=now if terminal else None,
            resolution_action=action,
            resolved_by_operation_id=resolved_by_operation_id,
            resolution_note=note,
        )
        return self.model_copy(
            update={
                "resolution": resolution,
                "resolved_at": now if terminal else None,
                "updated_at": now,
            }
        )


def error_fingerprint(
    *,
    component: ErrorComponent,
    error_type: ErrorType,
    provider: str | None,
    model: str | None,
    diagnostic_code: str,
    provider_error_code: str | None,
) -> str:
    """Sanitized grouping key from normalized fields only (docs/02 §12)."""
    parts = (
        component.value,
        error_type.value,
        provider or "",
        model or "",
        diagnostic_code,
        provider_error_code or "",
    )
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def error_event_from_failure(
    *,
    error_id: str,
    failure: NormalizedFailure,
    correlation_id: str,
    environment: AgentConfigEnvironment,
    recorded_at: datetime,
    adapter_version: str | None = None,
    model: str | None = None,
    logical_request_id: str | None = None,
    attempt_number: int | None = None,
) -> ErrorEventRecord:
    """Map a normalized failure onto the durable error record with safe defaults.

    ``logical_request_id``/``attempt_number`` keep the error correlated with
    its retry group and attempt (docs/02 §12; WP11).
    """
    expected = failure.error_type is ErrorType.CANCELLATION
    diagnostic_code = f"{failure.component.value}.{failure.error_type.value}"
    context = None
    if failure.provider is not None and adapter_version is not None:
        context = ErrorProviderContext(
            provider=failure.provider,
            model=model,
            adapter_version=adapter_version,
            http_status=failure.status_code
            if failure.status_code and 100 <= failure.status_code <= 599
            else None,
        )
    provider_side = failure.component in _PROVIDER_COMPONENTS and failure.provider is not None
    return ErrorEventRecord(
        error_id=error_id,
        session_id=failure.session_id,
        turn_id=failure.turn_id,
        operation_id=failure.operation_id,
        logical_request_id=logical_request_id,
        correlation_id=correlation_id,
        error_fingerprint=error_fingerprint(
            component=failure.component,
            error_type=failure.error_type,
            provider=failure.provider,
            model=model,
            diagnostic_code=diagnostic_code,
            provider_error_code=None,
        ),
        error_type=failure.error_type,
        category=failure.category,
        severity=ErrorSeverity.INFO if expected else ErrorSeverity.ERROR,
        component=failure.component,
        origin=ErrorOrigin.PROVIDER if provider_side else ErrorOrigin.APPLICATION,
        failure_phase=failure.failure_phase,
        provider_context=context,
        is_expected=expected,
        counts_toward_failure_rate=not expected,
        retry=ErrorRetry(
            retryable=failure.retryable,
            retry_scheduled=False,
            retry_attempt_number=attempt_number,
        ),
        fallback=ErrorFallback(
            attempted=failure.fallback_succeeded is not None,
            succeeded=bool(failure.fallback_succeeded),
        ),
        impact=ErrorImpact(
            user_affected=failure.user_affected,
            session_impact="none",
            turn_impact="failed" if failure.turn_id and not expected else "none",
            audio_impact="none",
            cost_may_be_incurred=provider_side,
        ),
        resolution=ErrorResolution(
            status=ResolutionStatus.IGNORED_EXPECTED if expected else ResolutionStatus.OPEN,
            revision=0,
        ),
        diagnostic_code=diagnostic_code,
        message_safe=failure.safe_message,
        occurred_at=failure.occurred_at,
        detected_at=failure.occurred_at,
        recorded_at=recorded_at,
        updated_at=recorded_at,
        environment=environment,
    )
