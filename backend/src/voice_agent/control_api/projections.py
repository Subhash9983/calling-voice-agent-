"""Browser-safe projections from domain/port shapes to API views (docs/04 §5-§14).

Each function selects an explicit allowlist of fields. Persistence documents,
system instructions, credential references, provider options, endpoints,
join-token evidence, and fingerprints are never projected.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from voice_agent.contracts.cost import EvidenceStatus
from voice_agent.contracts.enums import CalculationStatus, OperationComponent
from voice_agent.contracts.events import EventSeverity, browser_safe_view
from voice_agent.control_api.schemas.diagnostics import (
    MAX_ERROR_MESSAGE_CHARS,
    CostBreakdownView,
    CostComponentView,
    ErrorItem,
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
from voice_agent.costing.calculator import round_for_report
from voice_agent.costing.daily_spend import line_inr
from voice_agent.costing.reconciliation import ComponentCost
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.cost_entry import AggregationBehavior, CostEntryRecord
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.feedback import FeedbackRecord
from voice_agent.events_and_latency.evidence_types import SessionEvidence
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


def _cost_summary(record: SessionRecord, evidence: SessionEvidence | None) -> CostSummaryView:
    cost = None if evidence is None else evidence.cost
    total = None if cost is None else cost.session_total_usd
    return CostSummaryView(
        currency=record.cost_currency,
        rate_card_version=record.cost_rate_card_version,
        calculation_status=CalculationStatus.UNAVAILABLE if cost is None else cost.session_status,
        calculation_run_id=None if cost is None else cost.session_run_id,
        estimated_total_usd=None if total is None else decimal_text(total),
        reconciled=None if cost is None or total is None else cost.reconciled,
    )


def _error_counts(evidence: SessionEvidence) -> dict[str, int]:
    summary = evidence.error_summary
    return {
        "total": summary.total,
        "recoverable": summary.recoverable,
        "unrecoverable": summary.unrecoverable,
        "recovered": summary.recovered,
    }


def session_view(
    record: SessionRecord, name: str | None, evidence: SessionEvidence | None = None
) -> SessionView:
    """Summary counters/latency/cost are derived from stored child records (WP11)."""
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
        turn_summary=None if evidence is None else dict(evidence.turn_summary),
        error_summary=None if evidence is None else _error_counts(evidence),
        latency_summary=(
            None
            if evidence is None
            else {name: stats.to_dict() for name, stats in evidence.latency.items()}
        ),
        cost_summary=_cost_summary(record, evidence),
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


def decimal_text(value: Decimal) -> str:
    """Plain (never scientific) decimal text for money/quantities in API output."""
    return format(value, "f")


def operation_item(view: OperationView, cost: Decimal | None = None) -> OperationItem:
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
        estimated_cost=None if cost is None else decimal_text(cost),
        error_type=None if failure is None else failure.error_type,
        retryable=None if failure is None else failure.retryable,
        created_at=view.created_at,
        started_at=operation.started_at,
        time_to_first_result_ms=operation.time_to_first_result_ms,
        provider_duration_ms=operation.provider_duration_ms,
        total_duration_ms=operation.total_duration_ms,
    )


def _inr_display(lines: Sequence[CostEntryRecord]) -> str | None:
    """INR display from each line's original amount on its own dated card (Decision 069)."""
    total = Decimal(0)
    for line in lines:
        amount = line_inr(line)
        if amount is None:
            return None
        total += amount
    return decimal_text(round_for_report(total))


def cost_breakdown(
    lines: Sequence[CostEntryRecord], components: Sequence[ComponentCost] = ()
) -> CostBreakdownView:
    """Charge lines only contribute; allocation rows are never double counted.

    ``components`` (from the attempt-level reconciliation) supplies the
    retry/failure-related share of each component (docs/04 §14).
    """
    charges = [line for line in lines if line.aggregation_behavior is AggregationBehavior.CHARGE]
    groups: dict[tuple[str, str], Decimal] = {}
    for line in charges:
        key = (line.component.value, line.provider_identity.provider)
        groups[key] = groups.get(key, Decimal(0)) + line.currency_conversion.converted_net_cost
    retried = {(c.component, c.provider): c.retry_or_failure_usd for c in components}
    first = lines[0]
    estimated = any(line.evidence_status is EvidenceStatus.ESTIMATED for line in charges)
    return CostBreakdownView(
        calculation_run_id=first.calculation_run_id,
        calculation_status=first.calculation_status,
        evidence_status="estimated" if estimated else "usage_based",
        rate_card_version=first.rate.rate_card_version,
        total_usd=decimal_text(sum(groups.values(), Decimal(0))),
        total_inr_display=_inr_display(charges),
        retry_or_failure_usd=decimal_text(sum(retried.values(), Decimal(0))),
        components=tuple(
            CostComponentView(
                component=OperationComponent(component),
                label=display_label(component, provider),
                amount_usd=decimal_text(amount),
                retry_or_failure_related=retried.get((component, provider), Decimal(0)) > 0,
                retry_or_failure_usd=decimal_text(retried.get((component, provider), Decimal(0))),
            )
            for (component, provider), amount in sorted(groups.items())
        ),
        calculated_at=first.calculated_at,
    )


def error_item(record: ErrorEventRecord, recovered_requests: frozenset[str]) -> ErrorItem:
    """Safe allowlist only: no provider context, safe details, or restricted references."""
    request = record.logical_request_id
    return ErrorItem(
        error_id=record.error_id,
        diagnostic_code=record.diagnostic_code,
        component=record.component,
        error_type=record.error_type,
        category=record.category,
        severity=EventSeverity(record.severity.value),
        retryable=record.retry.retryable,
        retry_attempt_number=record.retry.retry_attempt_number,
        fallback_attempted=record.fallback.attempted,
        recovered=None if request is None else request in recovered_requests,
        user_affected=record.impact.user_affected,
        turn_id=record.turn_id,
        operation_id=record.operation_id,
        safe_message=record.message_safe[:MAX_ERROR_MESSAGE_CHARS],
        occurred_at=record.occurred_at,
    )


def feedback_receipt(record: FeedbackRecord) -> FeedbackReceipt:
    return FeedbackReceipt(
        feedback_id=record.feedback_id,
        client_submission_id=record.client_submission_id,
        session_id=record.session_id,
        target_type=record.content.target_type,
        created_at=record.created_at,
    )
