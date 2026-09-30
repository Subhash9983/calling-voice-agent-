"""``conversation_turns`` and ``provider_operations`` documents (docs/02 §7, §8).

The worker domain models omit per-session context (correlation ID, config,
environment, adapter versions); a :class:`WriteContext` supplies it. Failure
details stay in ``error_events``: an operation stores only its bounded
``failure_summary``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
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
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.records_common import (
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    RedactionStatus,
    Revision,
    SafeNote,
    bounded_container,
)
from voice_agent.domain.turn import ConversationTurn, InterruptionSummary
from voice_agent.privacy_and_retention.expiry import CONTENT_POLICY_VERSION

TIMELINE_SCHEMA_VERSION: Final = 1
MAX_TRANSCRIPT_CHARS = 10_000
MAX_RESPONSE_CHARS = 20_000
FAILURE_DETAIL_MESSAGE: Final = "Failure details are recorded in error_events."
Summary = Annotated[dict[str, JsonValue], bounded_container(8 * 1024, 50)]
ResponseText = Annotated[str, Field(max_length=MAX_RESPONSE_CHARS)]
TranscriptText = Annotated[str, Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARS)]
Ms = NonNegativeInt


@dataclass(frozen=True, slots=True)
class WriteContext:
    """Per-session context the worker domain models do not carry."""

    session_id: str
    correlation_id: str
    agent_config_id: str
    environment: AgentConfigEnvironment
    adapter_versions: Mapping[OperationComponent, str] = field(default_factory=dict)


class StoredUsageUnit(StrEnum):
    """Approved ``provider_operations.usage`` units (docs/02 §8)."""

    CONNECTED_AUDIO_SECONDS = "connected_audio_seconds"
    TRANSCRIBED_AUDIO_SECONDS = "transcribed_audio_seconds"
    INPUT_TOKENS = "input_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    SYNTHESIZED_CHARACTERS = "synthesized_characters"
    GENERATED_AUDIO_SECONDS = "generated_audio_seconds"
    TRANSPORT_SESSION_SECONDS = "transport_session_seconds"
    RECORDED_AUDIO_SECONDS = "recorded_audio_seconds"
    REQUESTS = "requests"


# ------------------------------------------------------------------ turns --
class UserInputDoc(RecordModel):
    input_mode: Literal["voice", "text"]
    final_transcript: TranscriptText | None = None
    language: ResponseLanguage | None = None
    transcript_source: Literal["stt", "typed", "imported"] | None = None
    stt_confidence: Decimal | None = None
    audio_duration_ms: Ms | None = None
    word_count: NonNegativeInt | None = None
    primary_stt_operation_id: CanonicalId | None = None


class AgentResponseDoc(RecordModel):
    generated_text: ResponseText
    synthesized_text: ResponseText
    spoken_text: ResponseText
    language: ResponseLanguage | None = None
    primary_llm_operation_id: CanonicalId | None = None
    primary_tts_operation_id: CanonicalId | None = None
    spoken_text_accuracy: SpokenTextAccuracy


class RouteSummaryDoc(RecordModel):
    engine_type: Literal["general_llm", "knowledge_engine", "fallback"]
    fallback_used: bool
    retrieval_used: bool
    tool_call_count: NonNegativeInt
    fallback_reason: ShortLabel | None = None


class InterruptionSummaryDoc(RecordModel):
    detected_count: NonNegativeInt
    accepted: bool
    false_interruption_suppressed_count: NonNegativeInt
    accepted_at: UtcDatetime | None = None
    phase: InterruptionPhase | None = None
    reason: InterruptionReason | None = None
    playback_stopped_at: UtcDatetime | None = None
    audio_played_ms: Ms | None = None
    interruption_latency_ms: Ms | None = None


class TurnLatencyDoc(RecordModel):
    user_speech_duration_ms: Ms | None = None
    stt_finalization_ms: Ms | None = None
    llm_first_token_ms: Ms | None = None
    llm_total_ms: Ms | None = None
    tts_first_audio_ms: Ms | None = None
    first_audible_response_ms: Ms | None = None
    turn_completion_ms: Ms | None = None
    interruption_latency_ms: Ms | None = None


class TurnCostSummaryDoc(RecordModel):
    currency: ShortLabel
    rate_card_version: ExternalIdentifier
    calculation_status: CalculationStatus
    calculation_run_id: CanonicalId | None = None
    stt_cost: Decimal | None = None
    llm_cost: Decimal | None = None
    tts_cost: Decimal | None = None
    transport_allocated_cost: Decimal | None = None
    total_cost: Decimal | None = None


class ConversationTurnDocument(RecordModel):
    turn_id: CanonicalId
    session_id: CanonicalId
    sequence_number: PositiveInt
    correlation_id: ExternalIdentifier
    agent_config_id: CanonicalId
    environment: AgentConfigEnvironment
    schema_version: Literal[1] = TIMELINE_SCHEMA_VERSION
    status: TurnStatus
    status_revision: Revision
    failure_error_id: CanonicalId | None = None
    abandonment_reason: ShortLabel | None = None
    response_finish_reason: FinishReason | None = None
    input_disposition: InputDisposition
    response_completion_status: ResponseCompletionStatus
    user_input: UserInputDoc
    agent_response: AgentResponseDoc
    route_summary: RouteSummaryDoc
    interruption_summary: InterruptionSummaryDoc
    created_at: UtcDatetime
    updated_at: UtcDatetime
    user_speech_started_at: UtcDatetime | None = None
    user_speech_ended_at: UtcDatetime | None = None
    transcript_final_at: UtcDatetime | None = None
    response_generation_started_at: UtcDatetime | None = None
    response_text_completed_at: UtcDatetime | None = None
    playback_started_at: UtcDatetime | None = None
    playback_ended_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    latency_summary: TurnLatencyDoc | None = None
    cost_summary: TurnCostSummaryDoc | None = None
    content_policy_version: ShortLabel
    redaction_status: RedactionStatus


_NOT_ACCEPTED: Final = ["pending", "empty", "unusable", "timed_out"]
TURN_ACCEPTED_RULE: Final = {
    "anyOf": [
        {"properties": {"input_disposition": {"enum": _NOT_ACCEPTED}}},
        {
            "properties": {
                "input_disposition": {"enum": ["accepted"]},
                "user_input": {"required": ["final_transcript", "transcript_source"]},
            }
        },
    ]
}


def turn_document(
    turn: ConversationTurn, context: WriteContext, *, created_at: datetime, updated_at: datetime
) -> ConversationTurnDocument:
    accepted = turn.input_disposition is InputDisposition.ACCEPTED
    interruption = turn.interruption
    return ConversationTurnDocument(
        turn_id=turn.turn_id,
        session_id=turn.session_id,
        sequence_number=turn.sequence_number,
        correlation_id=context.correlation_id,
        agent_config_id=context.agent_config_id,
        environment=context.environment,
        status=turn.status,
        status_revision=turn.status_revision,
        response_finish_reason=turn.response_finish_reason,
        input_disposition=turn.input_disposition,
        response_completion_status=turn.response_completion_status,
        user_input=UserInputDoc(
            input_mode="voice",
            final_transcript=turn.final_transcript,
            language=turn.language,
            transcript_source="stt" if accepted else None,
        ),
        agent_response=AgentResponseDoc(
            generated_text=turn.generated_text,
            synthesized_text=turn.synthesized_text,
            spoken_text=turn.spoken_text,
            language=turn.language,
            spoken_text_accuracy=turn.spoken_text_accuracy,
        ),
        route_summary=RouteSummaryDoc(
            engine_type="general_llm",
            fallback_used=turn.fallback_used,
            retrieval_used=False,
            tool_call_count=0,
        ),
        interruption_summary=InterruptionSummaryDoc(
            detected_count=interruption.detected_count,
            accepted=interruption.accepted,
            false_interruption_suppressed_count=interruption.false_interruption_suppressed_count,
            phase=interruption.phase,
            reason=interruption.reason,
        ),
        created_at=created_at,
        updated_at=updated_at,
        content_policy_version=CONTENT_POLICY_VERSION,
        redaction_status=RedactionStatus.NOT_REQUIRED,
    )


def turn_from_document(doc: ConversationTurnDocument) -> ConversationTurn:
    summary = doc.interruption_summary
    return ConversationTurn(
        turn_id=doc.turn_id,
        session_id=doc.session_id,
        sequence_number=doc.sequence_number,
        status=doc.status,
        status_revision=doc.status_revision,
        input_disposition=doc.input_disposition,
        response_completion_status=doc.response_completion_status,
        response_finish_reason=doc.response_finish_reason,
        final_transcript=doc.user_input.final_transcript,
        language=doc.user_input.language or doc.agent_response.language,
        generated_text=doc.agent_response.generated_text,
        synthesized_text=doc.agent_response.synthesized_text,
        spoken_text=doc.agent_response.spoken_text,
        spoken_text_accuracy=doc.agent_response.spoken_text_accuracy,
        fallback_used=doc.route_summary.fallback_used,
        interruption=InterruptionSummary(
            detected_count=summary.detected_count,
            accepted=summary.accepted,
            false_interruption_suppressed_count=summary.false_interruption_suppressed_count,
            phase=summary.phase,
            reason=summary.reason,
        ),
    )


# ------------------------------------------------------------- operations --
class ProviderIdentityDoc(RecordModel):
    provider: ShortLabel
    adapter_version: ExternalIdentifier
    model: ExternalIdentifier | None = None
    provider_api_version: ShortLabel | None = None
    region_label: ShortLabel | None = None
    voice_id: ExternalIdentifier | None = None
    provider_request_id: ExternalIdentifier | None = None


class UsageItemDoc(RecordModel):
    unit: StoredUsageUnit
    quantity: Annotated[Decimal, Field(ge=0)]
    source: UsageSource
    estimated: bool


class OperationUsageDoc(RecordModel):
    reporting_status: UsageReportingStatus
    items: Annotated[tuple[UsageItemDoc, ...], Field(max_length=20)] = ()


class OperationCostSummaryDoc(RecordModel):
    currency: ShortLabel
    rate_card_version: ExternalIdentifier
    calculation_status: CalculationStatus
    calculation_run_id: CanonicalId | None = None
    estimated_total: Decimal | None = None
    provider_reported_total: Decimal | None = None
    reconciliation_status: Literal["not_checked", "matched", "mismatch", "unavailable"] | None = (
        None
    )
    calculated_at: UtcDatetime | None = None


class RetryDoc(RecordModel):
    is_retry: bool
    retryable: bool
    retry_reason: ShortLabel | None = None
    backoff_ms: Ms | None = None
    next_operation_id: CanonicalId | None = None


class CancellationDoc(RecordModel):
    requested: bool
    worker_generation: PositiveInt
    requested_at: UtcDatetime | None = None
    reason: ShortLabel | None = None
    acknowledged_at: UtcDatetime | None = None
    requested_by: Literal["session", "turn", "user_interruption", "timeout", "system"] | None = None


class FailureSummaryDoc(RecordModel):
    error_id: CanonicalId | None = None
    error_type: ErrorType
    retryable: bool
    provider_status_code: Annotated[int, Field(strict=True, ge=0, le=65535)] | None = None
    user_affected: bool
    fallback_succeeded: bool


class ProviderOperationDocument(RecordModel):
    operation_id: CanonicalId
    logical_request_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    parent_operation_id: CanonicalId | None = None
    previous_attempt_operation_id: CanonicalId | None = None
    attempt_number: PositiveInt
    agent_config_id: CanonicalId
    environment: AgentConfigEnvironment
    schema_version: Literal[1] = TIMELINE_SCHEMA_VERSION
    component: OperationComponent
    operation_type: ShortLabel
    streaming: bool
    provider_identity: ProviderIdentityDoc
    status: OperationStatus
    status_revision: Revision
    result_disposition: ResultDisposition
    created_at: UtcDatetime
    updated_at: UtcDatetime
    queued_at: UtcDatetime | None = None
    started_at: UtcDatetime | None = None
    first_result_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    cancelled_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    queue_duration_ms: Ms | None = None
    time_to_first_result_ms: Ms | None = None
    provider_duration_ms: Ms | None = None
    total_duration_ms: Ms | None = None
    request_summary: Summary | None = None
    result_summary: Summary | None = None
    usage: OperationUsageDoc
    cost_summary: OperationCostSummaryDoc | None = None
    retry: RetryDoc
    cancellation: CancellationDoc
    failure_summary: FailureSummaryDoc | None = None
    fallback_triggered: bool
    fallback_from_operation_id: CanonicalId | None = None
    fallback_to_operation_id: CanonicalId | None = None
    fallback_reason: SafeNote | None = None
    safe_provider_metadata: (
        Annotated[dict[str, JsonValue], bounded_container(16 * 1024, 50)] | None
    ) = None


_STREAMING_COMPONENTS = frozenset(
    {OperationComponent.STT, OperationComponent.CONVERSATION_ENGINE, OperationComponent.TTS}
)
_TERMINAL_OPERATIONS = frozenset(
    {
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.TIMED_OUT,
        OperationStatus.CANCELLED,
    }
)


def _usage_doc(report: UsageReport) -> OperationUsageDoc:
    items = tuple(
        UsageItemDoc(
            unit=StoredUsageUnit(item.unit.value),
            quantity=item.quantity,
            source=item.source,
            estimated=item.estimated,
        )
        for item in report.items
    )
    return OperationUsageDoc(reporting_status=report.reporting_status, items=items)


def _failure_summary(failure: NormalizedFailure | None) -> FailureSummaryDoc | None:
    if failure is None:
        return None
    return FailureSummaryDoc(
        error_type=failure.error_type,
        retryable=failure.retryable,
        provider_status_code=failure.status_code,
        user_affected=failure.user_affected,
        fallback_succeeded=bool(failure.fallback_succeeded),
    )


def operation_document(
    operation: ProviderOperation,
    context: WriteContext,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> ProviderOperationDocument:
    adapter_version = context.adapter_versions.get(operation.component, "unknown")
    terminal = operation.status in _TERMINAL_OPERATIONS
    return ProviderOperationDocument(
        operation_id=operation.operation_id,
        logical_request_id=operation.logical_request_id,
        session_id=operation.session_id,
        turn_id=operation.turn_id,
        correlation_id=context.correlation_id,
        previous_attempt_operation_id=operation.previous_attempt_operation_id,
        attempt_number=operation.attempt_number,
        agent_config_id=context.agent_config_id,
        environment=context.environment,
        component=operation.component,
        operation_type=operation.operation_type,
        streaming=operation.component in _STREAMING_COMPONENTS,
        provider_identity=ProviderIdentityDoc(
            provider=operation.provider, adapter_version=adapter_version, model=operation.model
        ),
        status=operation.status,
        status_revision=operation.status_revision,
        result_disposition=operation.result_disposition,
        created_at=created_at,
        updated_at=updated_at,
        started_at=operation.started_at,
        first_result_at=operation.first_result_at,
        completed_at=updated_at if terminal else None,
        time_to_first_result_ms=operation.time_to_first_result_ms,
        provider_duration_ms=operation.provider_duration_ms,
        total_duration_ms=operation.total_duration_ms,
        result_summary=operation.result_summary,
        usage=_usage_doc(operation.usage),
        retry=RetryDoc(
            is_retry=operation.attempt_number > 1,
            retryable=bool(operation.failure and operation.failure.retryable),
        ),
        cancellation=CancellationDoc(
            requested=operation.cancellation_requested,
            worker_generation=operation.worker_generation,
        ),
        failure_summary=_failure_summary(operation.failure),
        fallback_triggered=False,
    )


def _failure_from_summary(doc: ProviderOperationDocument) -> NormalizedFailure | None:
    summary = doc.failure_summary
    if summary is None:
        return None
    return NormalizedFailure(
        component=ErrorComponent(doc.component.value),
        provider=doc.provider_identity.provider,
        error_type=summary.error_type,
        safe_message=FAILURE_DETAIL_MESSAGE,
        retryable=summary.retryable,
        status_code=summary.provider_status_code,
        session_id=doc.session_id,
        turn_id=doc.turn_id,
        operation_id=doc.operation_id,
        occurred_at=doc.completed_at or doc.updated_at,
        user_affected=summary.user_affected,
        fallback_succeeded=summary.fallback_succeeded,
    )


def operation_from_document(doc: ProviderOperationDocument) -> ProviderOperation:
    usage = (
        UsageReport.unavailable()
        if doc.usage.reporting_status is UsageReportingStatus.UNAVAILABLE
        else UsageReport(
            reporting_status=doc.usage.reporting_status,
            items=tuple(
                UsageItem(
                    unit=UsageUnit(item.unit.value),
                    quantity=item.quantity,
                    source=item.source,
                    estimated=item.estimated,
                )
                for item in doc.usage.items
            ),
        )
    )
    return ProviderOperation(
        operation_id=doc.operation_id,
        logical_request_id=doc.logical_request_id,
        session_id=doc.session_id,
        turn_id=doc.turn_id,
        component=doc.component,
        operation_type=doc.operation_type,
        provider=doc.provider_identity.provider,
        model=doc.provider_identity.model,
        attempt_number=doc.attempt_number,
        previous_attempt_operation_id=doc.previous_attempt_operation_id,
        worker_generation=doc.cancellation.worker_generation,
        status=doc.status,
        status_revision=doc.status_revision,
        result_disposition=doc.result_disposition,
        usage=usage,
        failure=_failure_from_summary(doc),
        cancellation_requested=doc.cancellation.requested,
        started_at=doc.started_at,
        first_result_at=doc.first_result_at,
        time_to_first_result_ms=doc.time_to_first_result_ms,
        provider_duration_ms=doc.provider_duration_ms,
        total_duration_ms=doc.total_duration_ms,
        result_summary=doc.result_summary,
    )
