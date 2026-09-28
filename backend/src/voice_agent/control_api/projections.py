"""Browser-safe projections from domain/port shapes to API views (docs/04 §5-§14).

Each function selects an explicit allowlist of fields. Persistence documents,
system instructions, credential references, provider options, endpoints,
join-token evidence, and fingerprints are never projected.
"""

from __future__ import annotations

from voice_agent.contracts.events import browser_safe_view
from voice_agent.control_api.schemas.diagnostics import (
    EventItem,
    InterruptionView,
    OperationItem,
    TurnItem,
    UsageItemView,
    UsageView,
)
from voice_agent.control_api.schemas.engagement import FeedbackReceipt
from voice_agent.control_api.schemas.sessions import (
    AgentConfigFeatures,
    AgentConfigView,
    ConfigurationLabels,
    CostSummaryView,
    EndSessionData,
    LanguageSummaryView,
    RecordingView,
    SessionBrief,
    SessionListItem,
    SessionView,
)
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.feedback import FeedbackRecord
from voice_agent.ports.control_plane import EventRecord, OperationView, TurnView
from voice_agent.provider_registry.display import display_label


def agent_config_view(config: AgentConfig) -> AgentConfigView:
    return AgentConfigView(
        agent_config_id=config.agent_config_id,
        agent_id=config.agent_id,
        name=config.name,
        description=config.description,
        version=config.version,
        transport=display_label("transport", config.transport.provider),
        stt=display_label("stt", config.stt.provider),
        conversation_engine=display_label(
            "conversation_engine", config.conversation_engine.provider
        ),
        tts=display_label("tts", config.tts.provider),
        language_mode=config.stt.language_mode,
        features=AgentConfigFeatures(
            partial_transcripts=config.stt.partial_transcripts,
            interruptions=config.turn_handling.interruptions_enabled,
        ),
    )


def configuration_labels(record: SessionRecord, name: str | None) -> ConfigurationLabels:
    snapshot = record.provider_snapshot
    return ConfigurationLabels(
        agent_config_id=record.agent_config_id,
        name=name,
        version=record.agent_config_version,
        transport=display_label("transport", snapshot.transport.provider),
        stt=display_label("stt", snapshot.stt.provider),
        conversation_engine=display_label(
            "conversation_engine", snapshot.conversation_engine.provider
        ),
        tts=display_label("tts", snapshot.tts.provider),
    )


def session_brief(record: SessionRecord) -> SessionBrief:
    return SessionBrief(
        session_id=record.session_id,
        status=record.status,
        agent_activity_state=record.agent_activity_state,
        created_at=record.created_at,
        maximum_session_ms=record.maximum_session_ms,
    )


def session_view(record: SessionRecord, name: str | None) -> SessionView:
    return SessionView(
        session_id=record.session_id,
        status=record.status,
        agent_activity_state=record.agent_activity_state,
        revision=record.state_revision,
        configuration=configuration_labels(record, name),
        created_at=record.created_at,
        updated_at=record.updated_at,
        connecting_at=record.connecting_at,
        ending_at=record.ending_at,
        ended_at=record.ended_at,
        maximum_session_ms=record.maximum_session_ms,
        language_summary=LanguageSummaryView(language_mode=record.language_mode),
        recording=RecordingView(mode=record.recording_mode, status=record.recording_status),
        termination_requested=record.termination_request is not None,
        disconnect_reason=record.disconnect_reason,
        cost_summary=CostSummaryView(
            currency=record.cost_currency,
            rate_card_version=record.cost_rate_card_version,
            calculation_status="unavailable",
        ),
    )


def session_list_item(record: SessionRecord) -> SessionListItem:
    return SessionListItem(
        session_id=record.session_id,
        status=record.status,
        agent_config_id=record.agent_config_id,
        agent_config_version=record.agent_config_version,
        created_at=record.created_at,
        ended_at=record.ended_at,
        disconnect_reason=record.disconnect_reason,
    )


def end_session_data(record: SessionRecord) -> EndSessionData:
    request = record.termination_request
    return EndSessionData(
        session_id=record.session_id,
        status=record.status,
        revision=record.state_revision,
        termination_request_revision=None if request is None else request.revision,
        disconnect_reason=record.disconnect_reason if record.is_terminal else None,
    )


def turn_item(view: TurnView) -> TurnItem:
    turn = view.turn
    interruption = turn.interruption
    return TurnItem(
        turn_id=turn.turn_id,
        sequence_number=turn.sequence_number,
        status=turn.status,
        input_disposition=turn.input_disposition,
        final_transcript=turn.final_transcript,
        language=turn.language,
        generated_text=turn.generated_text,
        synthesized_text=turn.synthesized_text,
        spoken_text=turn.spoken_text,
        spoken_text_accuracy=turn.spoken_text_accuracy,
        response_completion_status=turn.response_completion_status,
        response_finish_reason=turn.response_finish_reason,
        fallback_used=turn.fallback_used,
        interruption=InterruptionView(
            detected_count=interruption.detected_count,
            accepted=interruption.accepted,
            false_interruption_suppressed_count=interruption.false_interruption_suppressed_count,
            phase=interruption.phase,
            reason=interruption.reason,
        ),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def event_item(record: EventRecord) -> EventItem | None:
    """Only browser-safe durable events with an allocated sequence are exposed."""
    envelope = record.envelope
    safe = browser_safe_view(envelope)
    if safe is None or envelope.sequence_number is None:
        return None
    return EventItem(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        category=envelope.category,
        severity=record.severity,
        sequence_number=envelope.sequence_number,
        occurred_at=envelope.occurred_at,
        turn_id=envelope.turn_id,
        operation_id=envelope.operation_id,
        payload=safe["payload"],
    )


def operation_item(view: OperationView) -> OperationItem:
    operation = view.operation
    failure = operation.failure
    usage = UsageView(
        reporting_status=operation.usage.reporting_status,
        items=tuple(
            UsageItemView(
                unit=item.unit,
                quantity=str(item.quantity),
                source=item.source,
                estimated=item.estimated,
            )
            for item in operation.usage.items
        ),
    )
    return OperationItem(
        operation_id=operation.operation_id,
        turn_id=operation.turn_id,
        component=operation.component,
        provider=operation.provider,
        label=display_label(operation.component.value, operation.provider),
        attempt_number=operation.attempt_number,
        previous_attempt_operation_id=operation.previous_attempt_operation_id,
        status=operation.status,
        result_disposition=operation.result_disposition,
        usage=usage,
        estimated_cost=None,
        error_type=None if failure is None else failure.error_type,
        retryable=None if failure is None else failure.retryable,
        created_at=view.created_at,
    )


def feedback_receipt(record: FeedbackRecord) -> FeedbackReceipt:
    return FeedbackReceipt(
        feedback_id=record.feedback_id,
        client_submission_id=record.client_submission_id,
        session_id=record.session_id,
        target_type=record.content.target_type,
        created_at=record.created_at,
    )
