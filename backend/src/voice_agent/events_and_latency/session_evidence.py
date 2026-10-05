"""Reconstruct one session from its stored evidence (docs/02 §6-§12; docs/14 §17 WP11).

A read-only projection: session summary + turns + provider attempts + durable
events + cost lines + normalized errors -> a coherent timeline, turn/error
counters, latency summary, component usage, cost reconciliation, and
finalization evidence. Nothing here writes or calls a provider, and no
transcript, response, or prompt text is copied into the projection.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from voice_agent.contracts.cost import RateCard
from voice_agent.contracts.enums import OperationStatus, TurnStatus
from voice_agent.contracts.events import EventType
from voice_agent.costing.reconciliation import reconcile
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.evidence_audit import audit
from voice_agent.events_and_latency.evidence_types import (
    ErrorSummary,
    FinalizationEvidence,
    SessionEvidence,
    SessionEvidenceInput,
    TimelineEntry,
)
from voice_agent.events_and_latency.latency import latency_from_samples, turn_latency
from voice_agent.ports.control_plane import EventRecord
from voice_agent.privacy_and_retention.expiry import session_expires_at

TERMINAL_SESSION_EVENTS: Final = frozenset({EventType.SESSION_ENDED, EventType.SESSION_FAILED})
TURN_COUNTERS: Final = ("completed", "interrupted", "failed", "abandoned", "discarded")


def timeline(events: Sequence[EventRecord]) -> tuple[TimelineEntry, ...]:
    """Durable order: allocated sequence number, then occurrence time."""
    ordered = sorted(
        events, key=lambda r: (r.envelope.sequence_number or 0, r.envelope.occurred_at)
    )
    return tuple(
        TimelineEntry(
            sequence_number=r.envelope.sequence_number,
            occurred_at=r.envelope.occurred_at,
            event_type=r.envelope.event_type,
            severity=r.severity,
            turn_id=r.envelope.turn_id,
            operation_id=r.envelope.operation_id,
        )
        for r in ordered
    )


def turn_summary(turns: Sequence[ConversationTurn]) -> dict[str, int]:
    """docs/02 §6 ``turn_summary`` counters."""
    summary = {"total": len(turns)}
    for name in TURN_COUNTERS:
        summary[name] = sum(1 for turn in turns if turn.status is TurnStatus(name))
    return summary


def error_summary(
    errors: Sequence[ErrorEventRecord], operations: Sequence[ProviderOperation]
) -> ErrorSummary:
    """``recoverable`` = retryable classification; ``recovered`` = a later attempt succeeded."""
    succeeded = frozenset(
        op.logical_request_id for op in operations if op.status is OperationStatus.SUCCEEDED
    )
    recoverable = sum(1 for error in errors if error.retry.retryable)
    recovered = sum(1 for error in errors if error.logical_request_id in succeeded)
    last = max(errors, key=lambda error: error.occurred_at, default=None)
    return ErrorSummary(
        total=len(errors),
        recoverable=recoverable,
        unrecoverable=len(errors) - recoverable,
        recovered=recovered,
        last_error_id=None if last is None else last.error_id,
        by_type=dict(Counter(error.error_type.value for error in errors)),
    )


def usage_summary(
    operations: Sequence[ProviderOperation],
) -> tuple[dict[str, dict[str, Decimal]], tuple[str, ...]]:
    """Per-component usage over every attempt (retries are billable evidence too).

    Attempts whose usage is unavailable are listed, never counted as zero.
    """
    totals: dict[str, dict[str, Decimal]] = {}
    unavailable: list[str] = []
    for operation in operations:
        if not operation.usage.is_available:
            if operation.is_terminal:
                unavailable.append(operation.operation_id)
            continue
        component = totals.setdefault(operation.component.value, {})
        for item in operation.usage.items:
            unit = item.unit.value
            component[unit] = component.get(unit, Decimal(0)) + item.quantity
    return totals, tuple(unavailable)


def finalization_evidence(data: SessionEvidenceInput) -> FinalizationEvidence:
    session = data.session
    reason = session.disconnect_reason
    return FinalizationEvidence(
        terminal=session.is_terminal,
        ended_at=session.ended_at,
        disconnect_reason=None if reason is None else reason.value,
        terminal_event_recorded=any(
            r.envelope.event_type in TERMINAL_SESSION_EVENTS for r in data.events
        ),
        open_turns=sum(1 for turn in data.turns if not turn.is_terminal),
        open_operations=sum(1 for op in data.operations if not op.is_terminal),
        retention_expires_at=(
            None if session.ended_at is None else session_expires_at(session.ended_at)
        ),
    )


def session_latency_samples(data: SessionEvidenceInput) -> dict[str, tuple[int, ...]]:
    """Per-metric samples, one per turn (metrics without samples omitted)."""
    events: dict[str, list[EventRecord]] = {}
    for record in data.events:
        if record.envelope.turn_id is not None:
            events.setdefault(record.envelope.turn_id, []).append(record)
    attempts: dict[str, list[ProviderOperation]] = {}
    for operation in data.operations:
        if operation.turn_id is not None:
            attempts.setdefault(operation.turn_id, []).append(operation)
    collected: dict[str, list[int]] = {}
    for turn in data.turns:
        latency = turn_latency(
            turn,
            [r.envelope for r in events.get(turn.turn_id, [])],
            attempts.get(turn.turn_id, []),
        )
        for name, value in latency.samples.items():
            collected.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in collected.items()}


def build_session_evidence(data: SessionEvidenceInput, *, card: RateCard | None) -> SessionEvidence:
    """Assemble the projection; ``card`` is the session's own dated rate card."""
    usage, unavailable = usage_summary(data.operations)
    cost = reconcile(data.session.session_id, data.cost_entries, data.operations, card=card)
    finalization = finalization_evidence(data)
    samples = session_latency_samples(data)
    return SessionEvidence(
        session_id=data.session.session_id,
        status=data.session.status.value,
        created_at=data.session.created_at,
        rate_card_version=data.session.cost_rate_card_version,
        timeline=timeline(data.events),
        turn_summary=turn_summary(data.turns),
        error_summary=error_summary(data.errors, data.operations),
        latency=latency_from_samples(samples),
        latency_samples=samples,
        usage=usage,
        usage_unavailable_operation_ids=unavailable,
        cost=cost,
        finalization=finalization,
        issues=audit(data, cost, finalization),
    )
