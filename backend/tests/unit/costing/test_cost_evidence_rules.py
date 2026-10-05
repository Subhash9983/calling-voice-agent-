"""Cross-cutting WP11 cost-evidence rules over every Phase 0 component.

- unavailable usage or rates stay unavailable (or estimated), never zero;
- every attempt line is versioned to its dated rate card and correlated;
- LiveKit participant time is reported with its included-allowance basis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

import pytest

from voice_agent.contracts.cost import PricingBasis
from voice_agent.contracts.enums import CalculationStatus, OperationComponent, OperationStatus
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import (
    PHASE0_RATE_CARD_ID,
    ApprovedRateCards,
    phase0_rate_card,
)
from voice_agent.costing.reconciliation import UnpricedReason, reconcile
from voice_agent.costing.usage_normalization import UsageNormalizationError, transport_usage
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.operation import ProviderOperation
from voice_agent.ports.costing import RateLookup

SESSION = "00000000-0000-4000-8000-0000000000a1"
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


COMPONENTS = [
    (OperationComponent.STT, "deepgram", "nova-3", U.TRANSCRIBED_AUDIO_SECONDS),
    (OperationComponent.CONVERSATION_ENGINE, "openai", "gpt-6-luna", U.OUTPUT_TOKENS),
    (OperationComponent.TTS, "sarvam", "bulbul:v3", U.SYNTHESIZED_CHARACTERS),
    (OperationComponent.TRANSPORT, "livekit", None, U.TRANSPORT_SESSION_SECONDS),
]


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


def _attempt(component: OperationComponent, provider: str, model: str | None) -> ProviderOperation:
    return ProviderOperation(
        operation_id=_Ids().new_id(),
        logical_request_id=_Ids().new_id(),
        session_id=SESSION,
        component=component,
        operation_type="x",
        provider=provider,
        model=model,
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)


def _usage(unit: U, quantity: str = "100") -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.MEASURED,
        items=(UsageItem(unit=unit, quantity=Decimal(quantity), source=UsageSource.MEASURED),),
    )


@pytest.mark.parametrize(("component", "provider", "model", "unit"), COMPONENTS)
def test_unavailable_usage_is_never_priced_as_zero(
    component: OperationComponent, provider: str, model: str | None, unit: U
) -> None:
    ledger = _ledger()
    attempt = _attempt(component, provider, model).succeed(UsageReport.unavailable())

    assert ledger.settle(attempt) == ()
    assert ledger.session_run() == ()
    cost = reconcile(SESSION, [], [attempt], card=phase0_rate_card())
    assert cost.session_total_usd is None
    assert cost.session_status is CalculationStatus.UNAVAILABLE
    assert cost.unpriced[attempt.operation_id] is UnpricedReason.USAGE_UNAVAILABLE


@pytest.mark.parametrize(("component", "provider", "model", "unit"), COMPONENTS)
def test_an_unknown_model_rate_is_unavailable_not_zero(
    component: OperationComponent, provider: str, model: str | None, unit: U
) -> None:
    ledger = _ledger()
    attempt = _attempt(component, provider, "unapproved-model").succeed(_usage(unit))

    assert ledger.settle(attempt) == ()
    cost = reconcile(SESSION, [], [attempt], card=phase0_rate_card())
    assert cost.unpriced[attempt.operation_id] is UnpricedReason.RATE_UNAVAILABLE


@pytest.mark.parametrize(("component", "provider", "model", "unit"), COMPONENTS)
def test_every_priced_attempt_is_versioned_and_correlated(
    component: OperationComponent, provider: str, model: str | None, unit: U
) -> None:
    attempt = _attempt(component, provider, model).succeed(_usage(unit))

    [line] = _ledger().settle(attempt)

    assert line.rate.rate_card_version == PHASE0_RATE_CARD_ID
    assert line.rate_source.effective_date.date().isoformat() == "2026-09-26"
    assert (line.operation_id, line.logical_request_id) == (
        attempt.operation_id,
        attempt.logical_request_id,
    )


def test_livekit_participant_time_is_a_known_included_allowance() -> None:
    usage = transport_usage(connected_ms=90_000)
    attempt = _attempt(OperationComponent.TRANSPORT, "livekit", None).succeed(usage)

    [line] = _ledger().settle(attempt)

    assert usage.quantity_of(U.TRANSPORT_SESSION_SECONDS) == Decimal(180)  # 90 s x 2
    assert line.rate.pricing_basis is PricingBasis.INCLUDED_ALLOWANCE
    assert line.amounts.gross_cost == Decimal(0)
    assert line.quantity.billable_quantity == Decimal(3)  # participant minutes
    assert line.calculation_method == "estimated_quantity_x_public_rate"


@pytest.mark.parametrize(("connected_ms", "participants"), [(-1, 2), (1000, 0)])
def test_transport_usage_rejects_impossible_spans(connected_ms: int, participants: int) -> None:
    with pytest.raises(UsageNormalizationError):
        transport_usage(connected_ms=connected_ms, participants=participants)


def test_the_rate_lookup_port_returns_exact_versions_only() -> None:
    lookup = ApprovedRateCards()

    assert isinstance(lookup, RateLookup)
    card = lookup.rate_card(PHASE0_RATE_CARD_ID)
    assert card is not None
    assert card.rate_card_id == PHASE0_RATE_CARD_ID
    assert lookup.rate_card("phase0_rate_card_2026_10_01_v2") is None
