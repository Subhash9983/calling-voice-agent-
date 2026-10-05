"""Correlation/coherence audit and safe report views of session evidence (WP11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from tests.support.persistence_builders import make_config, make_session

from voice_agent.contracts.enums import (
    DisconnectReason,
    OperationComponent,
    OperationStatus,
    SessionStatus,
)
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID, phase0_rate_card
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.error_event import error_event_from_failure
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.evidence_report import aggregate_dict, session_dict
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.ports.control_plane import EventRecord

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_ids = count(1)


class _Ids:
    def new_id(self) -> str:
        return f"00000000-0000-4000-8000-{next(_ids):012x}"


class _Clock:
    def utc_now(self) -> datetime:
        return NOW

    def monotonic_ms(self) -> int:
        return 0


def _session(*, terminal: bool = True) -> SessionRecord:
    record = make_session(make_config()).model_copy(
        update={"cost_rate_card_version": PHASE0_RATE_CARD_ID}
    )
    if not terminal:
        return record
    return record.model_copy(
        update={
            "status": SessionStatus.ENDED,
            "ended_at": NOW,
            "disconnect_reason": DisconnectReason.USER_ENDED,
        }
    )


def _event(record: SessionRecord, event_type: EventType, **ids: str | None) -> EventRecord:
    envelope = EventEnvelope(
        event_id=_Ids().new_id(),
        event_type=event_type,
        occurred_at=NOW,
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        component="worker",
        producer_service="agent_worker",
        turn_id=ids.get("turn_id"),
        operation_id=ids.get("operation_id"),
    )
    return EventRecord(envelope, EventSeverity.INFO, NOW)


def _chars(quantity: int) -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.MEASURED,
        items=(
            UsageItem(
                unit=U.SYNTHESIZED_CHARACTERS,
                quantity=Decimal(quantity),
                source=UsageSource.MEASURED,
            ),
        ),
    )


def _tts(record: SessionRecord, turn_id: str | None) -> ProviderOperation:
    return ProviderOperation(
        operation_id=_Ids().new_id(),
        logical_request_id=_Ids().new_id(),
        session_id=record.session_id,
        turn_id=turn_id,
        component=OperationComponent.TTS,
        operation_type="synthesize_segment",
        provider="sarvam",
        model="bulbul:v3",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)


def _coherent(record: SessionRecord) -> SessionEvidenceInput:
    turn = ConversationTurn(
        turn_id=_Ids().new_id(), session_id=record.session_id, sequence_number=1
    )
    failed = _tts(record, turn.turn_id)
    failure = NormalizedFailure(
        component=ErrorComponent.TTS,
        provider="sarvam",
        error_type=ErrorType.PROVIDER_UNAVAILABLE,
        safe_message="Speech synthesis was unavailable.",
        retryable=True,
        session_id=record.session_id,
        turn_id=turn.turn_id,
        operation_id=failed.operation_id,
        occurred_at=NOW,
    )
    failed = failed.fail(failure, _chars(30))
    retried = failed.next_attempt(_Ids().new_id()).transition_to(OperationStatus.STARTED)
    retried = retried.succeed(_chars(30))
    ledger = AttemptCostLedger(
        LedgerContext(
            record.session_id, record.correlation_id, record.agent_config_id, record.environment
        ),
        card=phase0_rate_card(),
        ids=_Ids(),
        clock=_Clock(),
    )
    entries = [*ledger.settle(failed), *ledger.settle(retried), *ledger.session_run()]
    error = error_event_from_failure(
        error_id=_Ids().new_id(),
        failure=failure,
        correlation_id=record.correlation_id,
        environment=record.environment,
        recorded_at=NOW,
        logical_request_id=failed.logical_request_id,
        attempt_number=1,
    )
    return SessionEvidenceInput(
        session=record,
        turns=[turn.abandon()],
        events=[
            _event(
                record, EventType.TTS_FAILED, turn_id=turn.turn_id, operation_id=failed.operation_id
            ),
            _event(record, EventType.SESSION_ENDED),
        ],
        operations=[failed, retried],
        cost_entries=entries,
        errors=[error],
    )


def test_a_consistent_terminal_session_is_coherent() -> None:
    evidence = build_session_evidence(_coherent(_session()), card=phase0_rate_card())

    assert evidence.issues == ()
    assert evidence.coherent
    assert evidence.error_summary.recovered == 1
    assert evidence.error_summary.by_type == {"provider_unavailable": 1}
    assert evidence.finalization.retention_expires_at == NOW + timedelta(days=30)


def test_correlation_and_finalization_gaps_are_named() -> None:
    record = _session()
    data = _coherent(record)
    failed, retried = data.operations
    orphan = _tts(record, None)  # turn-scoped attempt without a turn, never settled
    broken_retry = retried.model_copy(update={"previous_attempt_operation_id": _Ids().new_id()})
    wrong_cost = data.cost_entries[0].model_copy(
        update={"logical_request_id": _Ids().new_id(), "correlation_id": "other"}
    )
    stray_error = data.errors[0].model_copy(update={"operation_id": _Ids().new_id()})
    stray_event = _event(record, EventType.TTS_FAILED, turn_id=_Ids().new_id())
    broken = SessionEvidenceInput(
        session=record,
        turns=[
            ConversationTurn(
                turn_id=_Ids().new_id(), session_id=record.session_id, sequence_number=1
            )
        ],
        events=[stray_event],
        operations=[failed, broken_retry, orphan],
        cost_entries=[wrong_cost, *data.cost_entries[1:]],
        errors=[stray_error],
        truncated=frozenset({"events"}),
    )

    issues = build_session_evidence(broken, card=phase0_rate_card()).issues
    codes = {issue.split(":", 1)[0] for issue in issues}

    assert codes >= {
        "evidence_truncated",
        "operation_missing_turn",
        "operation_unknown_turn",
        "retry_lineage_broken",
        "cost_correlation_id_mismatch",
        "cost_correlation_mismatch",
        "error_unknown_operation",
        "event_unknown_turn",
        "terminal_event_missing",
        "open_turns",
        "open_operations",
    }
    assert all(len(issue) <= 80 for issue in issues)  # codes and IDs only


def test_a_live_session_is_not_judged_on_finalization() -> None:
    data = _coherent(_session(terminal=False))
    live = SessionEvidenceInput(
        session=data.session,
        turns=data.turns,
        events=data.events[:1],
        operations=data.operations,
        cost_entries=[e for e in data.cost_entries if e.scope.value == "operation"],
        errors=data.errors,
    )

    evidence = build_session_evidence(live, card=phase0_rate_card())

    assert evidence.issues == ()
    assert evidence.finalization.retention_expires_at is None


def test_report_views_are_plain_safe_json() -> None:
    evidence = build_session_evidence(_coherent(_session()), card=phase0_rate_card())

    single = session_dict(evidence, include_timeline=True)
    summary = aggregate_dict([evidence, evidence])
    dumped = json.dumps([single, summary])

    assert single["cost"]["session_total_usd"] == "0.0018"
    assert single["cost"]["session_total_inr_display"] == "0.18"
    assert single["cost"]["retry_or_failure_usd"] == "0.0009"
    assert [entry["event_type"] for entry in single["timeline"]] == [
        "tts.failed",
        "session.ended",
    ]
    assert summary["sessions"] == 2
    assert summary["coherent_sessions"] == 2
    assert summary["cost"]["total_usd"] == "0.0036"
    assert summary["errors"] == {"total": 2, "by_type": {"provider_unavailable": 2}}
    assert "Speech synthesis was unavailable." not in dumped
