"""Attempt-level -> session-level cost runs without retry double counting (WP11).

Every terminal provider attempt is its own billable evidence (an
operation-scope run); the session-scope run prices each attempt exactly once.
A repeated settle is a no-op; late usage supersedes the attempt's earlier run
instead of adding to it (docs/02 §10, docs/15 §5, §12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

import pytest

from voice_agent.contracts.cost import EvidenceStatus
from voice_agent.contracts.enums import CalculationStatus, OperationComponent, OperationStatus
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID, phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope
from voice_agent.domain.operation import ProviderOperation

SESSION = "00000000-0000-4000-8000-0000000000a1"
TURN = "00000000-0000-4000-8000-0000000000b1"
CONFIG = "00000000-0000-4000-8000-0000000000c1"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._next = count(1)

    def new_id(self) -> str:
        return f"00000000-0000-4000-8000-{next(self._next):012x}"


class _Clock:
    def utc_now(self) -> datetime:
        return NOW

    def monotonic_ms(self) -> int:
        return 0


def _ledger() -> AttemptCostLedger:
    return AttemptCostLedger(
        LedgerContext(
            session_id=SESSION,
            correlation_id="wp11-ledger",
            agent_config_id=CONFIG,
            environment=AgentConfigEnvironment.DEVELOPMENT,
        ),
        card=phase0_rate_card(),
        ids=_Ids(),
        clock=_Clock(),
    )


def _tokens(input_tokens: int, output_tokens: int) -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(
                unit=U.INPUT_TOKENS,
                quantity=Decimal(input_tokens),
                source=UsageSource.PROVIDER_REPORTED,
            ),
            UsageItem(
                unit=U.OUTPUT_TOKENS,
                quantity=Decimal(output_tokens),
                source=UsageSource.PROVIDER_REPORTED,
            ),
        ),
    )


def _llm(
    operation_id: str, logical: str = "00000000-0000-4000-8000-0000000000d1"
) -> ProviderOperation:
    return ProviderOperation(
        operation_id=operation_id,
        logical_request_id=logical,
        session_id=SESSION,
        turn_id=TURN,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider="openai",
        model="gpt-6-luna",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)


def _failure(operation_id: str) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.CONVERSATION_ENGINE,
        provider="openai",
        error_type=ErrorType.PROVIDER_TIMEOUT,
        safe_message="The model did not answer in time.",
        retryable=True,
        session_id=SESSION,
        turn_id=TURN,
        operation_id=operation_id,
        occurred_at=NOW,
    )


def _total(entries: tuple[CostEntryRecord, ...]) -> Decimal:
    return sum((e.currency_conversion.converted_net_cost for e in entries), Decimal(0))


def _failed_then_retried() -> tuple[ProviderOperation, ProviderOperation]:
    first = _llm("00000000-0000-4000-8000-0000000000e1")
    failed = first.fail(_failure(first.operation_id), _tokens(2_000, 10))
    retry = first.next_attempt("00000000-0000-4000-8000-0000000000e2")
    succeeded = retry.transition_to(OperationStatus.STARTED).succeed(_tokens(2_000, 40))
    return failed, succeeded


def test_each_retry_attempt_is_its_own_operation_scope_run() -> None:
    ledger = _ledger()
    failed, succeeded = _failed_then_retried()

    first = ledger.settle(failed)
    second = ledger.settle(succeeded)

    for entries, operation in ((first, failed), (second, succeeded)):
        assert entries
        assert {e.scope for e in entries} == {CostScope.OPERATION}
        assert {e.operation_id for e in entries} == {operation.operation_id}
        assert {e.turn_id for e in entries} == {TURN}
        assert {e.logical_request_id for e in entries} == {operation.logical_request_id}
        assert {e.rate.rate_card_version for e in entries} == {PHASE0_RATE_CARD_ID}
        assert {e.calculation_version for e in entries} == {1}
    assert first[0].calculation_run_id != second[0].calculation_run_id
    # 2000 in x 0.10/1M + 10 out x 0.50/1M; then 40 out.
    assert _total(first) == Decimal("0.000205")
    assert _total(second) == Decimal("0.00022")


def test_session_run_counts_every_attempt_exactly_once() -> None:
    ledger = _ledger()
    failed, succeeded = _failed_then_retried()
    attempts = ledger.settle(failed) + ledger.settle(succeeded)

    session = ledger.session_run()

    assert {e.scope for e in session} == {CostScope.SESSION}
    assert {e.calculation_status for e in session} == {CalculationStatus.FINAL}
    assert _total(session) == _total(attempts) == Decimal("0.000425")


def test_a_repeated_settle_never_double_counts_the_session() -> None:
    ledger = _ledger()
    failed, succeeded = _failed_then_retried()
    ledger.settle(failed)
    ledger.settle(succeeded)

    replay = ledger.settle(succeeded)
    replay_again = ledger.settle(failed)
    session = ledger.session_run()

    assert replay == ()
    assert replay_again == ()
    assert _total(session) == Decimal("0.000425")


def test_late_usage_supersedes_the_attempt_and_the_session_run() -> None:
    ledger = _ledger()
    failed, succeeded = _failed_then_retried()
    ledger.settle(failed)
    original = ledger.settle(succeeded)
    first_session = ledger.session_run()

    late = ledger.settle(succeeded.with_late_usage(_tokens(2_000, 100)))
    second_session = ledger.session_run()

    assert {e.calculation_version for e in late} == {2}
    assert {e.supersedes_calculation_run_id for e in late} == {original[0].calculation_run_id}
    assert {e.calculation_version for e in second_session} == {2}
    assert {e.supersedes_calculation_run_id for e in second_session} == {
        first_session[0].calculation_run_id
    }
    # Only the latest usage counts: 0.000205 + (0.0002 + 0.00005).
    assert _total(second_session) == Decimal("0.000455")


def test_an_unchanged_ledger_writes_no_new_session_run() -> None:
    ledger = _ledger()
    ledger.settle(_failed_then_retried()[1])

    assert ledger.session_run()
    assert ledger.session_run() == ()


def test_unavailable_usage_is_never_priced_and_makes_the_session_partial() -> None:
    ledger = _ledger()
    priced = _failed_then_retried()[1]
    cancelled = _llm("00000000-0000-4000-8000-0000000000e3").cancel()

    assert ledger.settle(cancelled) == ()
    attempt = ledger.settle(priced)
    session = ledger.session_run()

    assert {e.calculation_status for e in session} == {CalculationStatus.PARTIAL}
    assert _total(session) == _total(attempt)
    assert ledger.unpriced_operation_ids == (cancelled.operation_id,)


def test_estimated_usage_stays_labelled_estimated() -> None:
    ledger = _ledger()
    estimate = UsageReport(
        reporting_status=UsageReportingStatus.ESTIMATED,
        items=(
            UsageItem(
                unit=U.OUTPUT_TOKENS,
                quantity=Decimal(12),
                source=UsageSource.ESTIMATED,
                estimated=True,
            ),
        ),
    )

    entries = ledger.settle(_llm("00000000-0000-4000-8000-0000000000e4").cancel(estimate))

    assert {e.evidence_status for e in entries} == {EvidenceStatus.ESTIMATED}
    assert {e.calculation_method for e in entries} == {"estimated_tokens_x_public_rate"}


def test_only_terminal_attempts_can_be_settled() -> None:
    with pytest.raises(ValueError, match="terminal"):
        _ledger().settle(_llm("00000000-0000-4000-8000-0000000000e5"))


def test_an_empty_ledger_has_no_session_run() -> None:
    assert _ledger().session_run() == ()
