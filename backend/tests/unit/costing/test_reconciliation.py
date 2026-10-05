"""Attempt/turn/session reconciliation from stored cost lines (docs/15 §5, §12; WP11)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

import pytest

from voice_agent.contracts.enums import CalculationStatus, OperationComponent, OperationStatus
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.costing.reconciliation import (
    UnpricedReason,
    difference_percent,
    latest_runs,
    reconcile,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope
from voice_agent.domain.operation import ProviderOperation

SESSION = "00000000-0000-4000-8000-0000000000a1"
TURN = "00000000-0000-4000-8000-0000000000b1"
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


def _ledger() -> AttemptCostLedger:
    return AttemptCostLedger(
        LedgerContext(
            SESSION,
            "wp11",
            "00000000-0000-4000-8000-0000000000c1",
            AgentConfigEnvironment.DEVELOPMENT,
        ),
        card=phase0_rate_card(),
        ids=_Ids(),
        clock=_Clock(),
    )


def _usage(unit: U, quantity: str) -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.MEASURED,
        items=(UsageItem(unit=unit, quantity=Decimal(quantity), source=UsageSource.MEASURED),),
    )


def _op(
    component: OperationComponent,
    provider: str,
    model: str | None,
    *,
    turn_id: str | None = TURN,
) -> ProviderOperation:
    return ProviderOperation(
        operation_id=_Ids().new_id(),
        logical_request_id=_Ids().new_id(),
        session_id=SESSION,
        turn_id=turn_id,
        component=component,
        operation_type="x",
        provider=provider,
        model=model,
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)


def _failure(op: ProviderOperation) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.TTS,
        error_type=ErrorType.PROVIDER_UNAVAILABLE,
        safe_message="unavailable",
        retryable=True,
        session_id=SESSION,
        operation_id=op.operation_id,
        occurred_at=NOW,
    )


def _scenario() -> tuple[list[CostEntryRecord], list[ProviderOperation]]:
    ledger = _ledger()
    stt = _op(OperationComponent.STT, "deepgram", "nova-3", turn_id=None).succeed(
        _usage(U.TRANSCRIBED_AUDIO_SECONDS, "120")
    )
    failed_tts = _op(OperationComponent.TTS, "sarvam", "bulbul:v3")
    failed_tts = failed_tts.fail(_failure(failed_tts), _usage(U.SYNTHESIZED_CHARACTERS, "50"))
    tts = _op(OperationComponent.TTS, "sarvam", "bulbul:v3").succeed(
        _usage(U.SYNTHESIZED_CHARACTERS, "50")
    )
    operations = [stt, failed_tts, tts]
    entries = [line for op in operations for line in ledger.settle(op)]
    entries += ledger.session_run()
    return entries, operations


def test_turn_session_and_attempt_totals_agree() -> None:
    entries, operations = _scenario()

    cost = reconcile(SESSION, entries, operations, card=phase0_rate_card())

    # STT 2 min x 0.0092 = 0.0184; TTS 2 x 50 chars x INR 0.003 x 0.01 = 0.003.
    assert cost.session_total_usd == Decimal("0.0214")
    assert cost.attempt_total_usd == Decimal("0.0214")
    assert cost.difference_percent == Decimal(0)
    assert cost.reconciled
    assert cost.turn_totals_usd == {TURN: Decimal("0.003")}
    assert cost.unattributed_usd == Decimal("0.0184")  # session-level STT stream
    assert cost.retry_or_failure_usd == Decimal("0.0015")
    assert cost.session_status is CalculationStatus.FINAL
    tts = next(c for c in cost.components if c.component == "tts")
    assert tts.retry_or_failure_usd == Decimal("0.0015")


def test_a_drifted_session_total_is_not_reconciled() -> None:
    entries, operations = _scenario()
    drifted = [
        e.model_copy(
            update={
                "currency_conversion": e.currency_conversion.model_copy(
                    update={"converted_net_cost": e.currency_conversion.converted_net_cost * 2}
                )
            }
        )
        if e.scope is CostScope.SESSION
        else e
        for e in entries
    ]

    cost = reconcile(SESSION, drifted, operations, card=phase0_rate_card())

    assert not cost.reconciled
    assert cost.difference_percent == Decimal(50)


def test_superseded_runs_are_never_added_together() -> None:
    ledger = _ledger()
    tts = _op(OperationComponent.TTS, "sarvam", "bulbul:v3").succeed(
        _usage(U.SYNTHESIZED_CHARACTERS, "50")
    )
    first = [*ledger.settle(tts), *ledger.session_run()]
    late = tts.with_late_usage(_usage(U.SYNTHESIZED_CHARACTERS, "100"))
    second = [*ledger.settle(late), *ledger.session_run()]

    cost = reconcile(SESSION, first + second, [late], card=phase0_rate_card())

    assert cost.session_total_usd == Decimal("0.003")
    assert cost.attempt_total_usd == Decimal("0.003")
    assert cost.superseded_runs == 2
    assert len(latest_runs(first + second)) == 2


def test_every_unpriced_attempt_is_classified_never_zero() -> None:
    mock = _op(OperationComponent.TTS, "mock_tts", "mock-tts-v1").succeed(
        _usage(U.SYNTHESIZED_CHARACTERS, "10")
    )
    evidence_only = _op(OperationComponent.STT, "deepgram", "nova-3").succeed(
        _usage(U.CONNECTED_AUDIO_SECONDS, "10")
    )
    unavailable = _op(OperationComponent.CONVERSATION_ENGINE, "openai", "gpt-6-luna").cancel()
    running = _op(OperationComponent.TTS, "sarvam", "bulbul:v3")
    operations = [mock, evidence_only, unavailable, running]

    cost = reconcile(SESSION, [], operations, card=phase0_rate_card())
    unknown_card = reconcile(SESSION, [], [mock], card=None)

    assert cost.unpriced == {
        mock.operation_id: UnpricedReason.RATE_UNAVAILABLE,
        evidence_only.operation_id: UnpricedReason.NOT_BILLABLE,
        unavailable.operation_id: UnpricedReason.USAGE_UNAVAILABLE,
        running.operation_id: UnpricedReason.NOT_TERMINAL,
    }
    assert unknown_card.unpriced == {mock.operation_id: UnpricedReason.RATE_CARD_UNKNOWN}
    assert cost.session_total_usd is None
    assert cost.attempt_total_usd is None
    assert cost.session_status is CalculationStatus.UNAVAILABLE
    assert cost.reconciled  # nothing priced on either side: unavailable, not zero


@pytest.mark.parametrize(
    ("session", "attempts", "expected"),
    [("0", "0", "0"), ("0", "1", "100"), ("100", "99", "1.0000"), ("3", "2", "33.3333")],
)
def test_difference_percent(session: str, attempts: str, expected: str) -> None:
    assert difference_percent(Decimal(session), Decimal(attempts)) == Decimal(expected)


def test_a_partially_priced_attempt_is_listed_not_hidden() -> None:
    usage = UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(unit=U.OUTPUT_TOKENS, quantity=Decimal(100), source=UsageSource.MEASURED),
            UsageItem(unit=U.REQUESTS, quantity=Decimal(1), source=UsageSource.MEASURED),
        ),
    )
    attempt = _op(OperationComponent.CONVERSATION_ENGINE, "openai", "gpt-6-luna").succeed(usage)
    ledger = _ledger()
    entries = [*ledger.settle(attempt), *ledger.session_run()]

    cost = reconcile(SESSION, entries, [attempt], card=phase0_rate_card())

    assert cost.session_status is CalculationStatus.PARTIAL
    assert cost.attempt_costs_usd[attempt.operation_id] == Decimal("0.00005")
    assert cost.unpriced == {attempt.operation_id: UnpricedReason.PARTIALLY_PRICED}
