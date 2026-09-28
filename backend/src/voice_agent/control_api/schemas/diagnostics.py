"""Turn, event, operation, error, and cost read contracts (docs/04 §10-§14).

Projections exclude provider payloads, hidden instructions, credentials,
headers, duplicated prompt content, raw exceptions, and restricted pricing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue

from voice_agent.contracts.base import CanonicalId
from voice_agent.contracts.enums import (
    CalculationStatus,
    FinishReason,
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
    ResponseCompletionStatus,
    ResponseLanguage,
    ResultDisposition,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.events import EventCategory, EventSeverity, EventType
from voice_agent.contracts.failures import ErrorCategory, ErrorComponent, ErrorType
from voice_agent.contracts.usage import UsageReportingStatus, UsageSource, UsageUnit
from voice_agent.control_api.schemas.common import ApiModel, Cursor

DEFAULT_TURN_PAGE, MAX_TURN_PAGE = 50, 100
DEFAULT_EVENT_PAGE, MAX_EVENT_PAGE = 100, 500
# docs/04 §12-§13 give no page sizes; the turn-page bounds are reused.
DEFAULT_DIAGNOSTIC_PAGE, MAX_DIAGNOSTIC_PAGE = 50, 100


class TurnListParams(ApiModel):
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_TURN_PAGE)] = DEFAULT_TURN_PAGE


class EventListParams(ApiModel):
    category: EventCategory | None = None
    severity: EventSeverity | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_EVENT_PAGE)] = DEFAULT_EVENT_PAGE


class OperationListParams(ApiModel):
    turn_id: CanonicalId | None = None
    component: OperationComponent | None = None
    status: OperationStatus | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_DIAGNOSTIC_PAGE)] = DEFAULT_DIAGNOSTIC_PAGE


class ErrorListParams(ApiModel):
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_DIAGNOSTIC_PAGE)] = DEFAULT_DIAGNOSTIC_PAGE


class InterruptionView(ApiModel):
    detected_count: int
    accepted: bool
    false_interruption_suppressed_count: int
    phase: InterruptionPhase | None
    reason: InterruptionReason | None


class TurnItem(ApiModel):
    turn_id: str
    sequence_number: int
    status: TurnStatus
    input_disposition: InputDisposition
    final_transcript: str | None
    language: ResponseLanguage | None
    generated_text: str
    synthesized_text: str
    spoken_text: str
    spoken_text_accuracy: SpokenTextAccuracy
    response_completion_status: ResponseCompletionStatus
    response_finish_reason: FinishReason | None
    fallback_used: bool
    interruption: InterruptionView
    created_at: datetime
    updated_at: datetime


class EventItem(ApiModel):
    event_id: str
    event_type: EventType
    category: EventCategory | None
    severity: EventSeverity
    sequence_number: int
    occurred_at: datetime
    turn_id: str | None
    operation_id: str | None
    payload: dict[str, JsonValue]


class UsageItemView(ApiModel):
    unit: UsageUnit
    quantity: str
    source: UsageSource
    estimated: bool


class UsageView(ApiModel):
    reporting_status: UsageReportingStatus
    items: tuple[UsageItemView, ...]


class OperationItem(ApiModel):
    operation_id: str
    turn_id: str | None
    component: OperationComponent
    provider: str
    label: str
    attempt_number: int
    previous_attempt_operation_id: str | None
    status: OperationStatus
    result_disposition: ResultDisposition
    usage: UsageView
    estimated_cost: str | None
    error_type: ErrorType | None
    retryable: bool | None
    created_at: datetime


class ErrorItem(ApiModel):
    """Safe error diagnostic (docs/04 §13); served once error_events exist (WP11)."""

    error_id: str
    component: ErrorComponent
    error_type: ErrorType
    category: ErrorCategory
    severity: EventSeverity
    retryable: bool
    recovered: bool | None
    user_affected: bool
    safe_message: Annotated[str, Field(max_length=500)]
    occurred_at: datetime


class CostComponentView(ApiModel):
    component: OperationComponent
    label: str
    amount_usd: str
    retry_or_failure_related: bool


class CostBreakdownView(ApiModel):
    """Latest successful calculation run (docs/04 §14); served from WP11."""

    calculation_run_id: str
    calculation_status: CalculationStatus
    total_usd: str
    total_inr_display: str | None
    components: tuple[CostComponentView, ...]
    calculated_at: datetime
