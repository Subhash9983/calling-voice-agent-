"""Evaluation usage/cost evidence through the WP11 durable evidence model.

There is no evaluation-specific cost tracker: a transcript harness attempt is
priced with the WP11 :class:`AttemptCostLedger` and reconciled with
:func:`reconcile`; a reliability/live result summarizes the same
:class:`CostReconciliation` that :func:`build_session_evidence` derives for a
real session. Missing usage or rates stay ``unavailable`` (never zero).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from voice_agent.contracts.cost import Currency, RateCard
from voice_agent.contracts.enums import CalculationStatus
from voice_agent.contracts.usage import UsageReportingStatus
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.calculator import round_for_report
from voice_agent.costing.reconciliation import CostReconciliation, reconcile
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.evaluation.result import UsageAndCost
from voice_agent.domain.operation import ProviderOperation
from voice_agent.ports.clock import Clock, IdGenerator

UsageStatus = Literal["provider_reported", "measured", "estimated", "unavailable"]
CalcStatus = Literal["pending", "partial", "final", "failed", "unavailable"]
ReconStatus = Literal["not_checked", "matched", "mismatch", "unavailable"]
MAX_REFERENCES: Final = 50
_USAGE_RANK: Final[dict[UsageReportingStatus, int]] = {
    UsageReportingStatus.PROVIDER_REPORTED: 0,
    UsageReportingStatus.MEASURED: 1,
    UsageReportingStatus.ESTIMATED: 2,
    UsageReportingStatus.UNAVAILABLE: 3,
}
_USAGE_LABEL: Final[dict[UsageReportingStatus, UsageStatus]] = {
    UsageReportingStatus.PROVIDER_REPORTED: "provider_reported",
    UsageReportingStatus.MEASURED: "measured",
    UsageReportingStatus.ESTIMATED: "estimated",
    UsageReportingStatus.UNAVAILABLE: "unavailable",
}
_CALC_LABEL: Final[dict[CalculationStatus, CalcStatus]] = {
    CalculationStatus.PENDING: "pending",
    CalculationStatus.PARTIAL: "partial",
    CalculationStatus.FINAL: "final",
    CalculationStatus.FAILED: "failed",
    CalculationStatus.UNAVAILABLE: "unavailable",
}


@dataclass(frozen=True, slots=True)
class CostEvidence:
    rate_card_id: str
    usage_status: UsageStatus
    calculation_status: CalcStatus
    reconciliation_status: ReconStatus
    net_cost_usd: Decimal | None = None
    marginal_cost_inr: Decimal | None = None
    calculation_run_id: str | None = None
    operation_ids: tuple[str, ...] = ()
    cost_entry_ids: tuple[str, ...] = ()
    unpriced: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def has_usage_evidence(self) -> bool:
        return bool(self.operation_ids)

    def to_usage_and_cost(self) -> UsageAndCost:
        return UsageAndCost(
            rate_card_id=self.rate_card_id,
            calculation_run_id=self.calculation_run_id,
            operation_ids=self.operation_ids[:MAX_REFERENCES],
            cost_entry_ids=self.cost_entry_ids[:MAX_REFERENCES],
            net_cost_usd=self.net_cost_usd,
            marginal_cost_inr=self.marginal_cost_inr,
            usage_status=self.usage_status,
            calculation_status=self.calculation_status,
            reconciliation_status=self.reconciliation_status,
        )


def _usage_status(operations: Sequence[ProviderOperation]) -> UsageStatus:
    if not operations:
        return "unavailable"
    worst = max(operations, key=lambda op: _USAGE_RANK[op.usage.reporting_status])
    return _USAGE_LABEL[worst.usage.reporting_status]


def _reconciliation_status(cost: CostReconciliation) -> ReconStatus:
    if cost.session_total_usd is None and cost.attempt_total_usd is None:
        return "unavailable"
    return "matched" if cost.reconciled else "mismatch"


def _calculation_status(cost: CostReconciliation) -> CalcStatus:
    if cost.session_total_usd is None:
        return "unavailable" if cost.attempt_total_usd is None else "partial"
    if cost.unpriced and cost.session_status is CalculationStatus.FINAL:
        return "partial"
    return _CALC_LABEL[cost.session_status]


def _inr(amount_usd: Decimal | None, card: RateCard | None) -> Decimal | None:
    fx = None if card is None else card.find_fx(Currency.USD, Currency.INR)
    if amount_usd is None or fx is None:
        return None
    return round_for_report(amount_usd * fx, 6)


def evidence_from_reconciliation(
    cost: CostReconciliation,
    *,
    operations: Sequence[ProviderOperation],
    entries: Sequence[CostEntryRecord],
    rate_card_id: str,
    card: RateCard | None,
) -> CostEvidence:
    total = cost.session_total_usd
    return CostEvidence(
        rate_card_id=rate_card_id,
        usage_status=_usage_status(operations),
        calculation_status=_calculation_status(cost),
        reconciliation_status=_reconciliation_status(cost),
        net_cost_usd=total,
        marginal_cost_inr=_inr(total, card),
        calculation_run_id=cost.session_run_id,
        operation_ids=tuple(op.operation_id for op in operations),
        cost_entry_ids=tuple(entry.cost_entry_id for entry in entries),
        unpriced=tuple(sorted((key, reason.value) for key, reason in cost.unpriced.items())),
    )


def price_attempts(
    operations: Sequence[ProviderOperation],
    *,
    context: LedgerContext,
    card: RateCard,
    ids: IdGenerator,
    clock: Clock,
) -> CostEvidence:
    """Price terminal harness attempts exactly as a session would, then reconcile."""
    ledger = AttemptCostLedger(context, card=card, ids=ids, clock=clock)
    entries: list[CostEntryRecord] = []
    for operation in operations:
        entries.extend(ledger.settle(operation))
    entries.extend(ledger.session_run())
    cost = reconcile(context.session_id, entries, operations, card=card)
    return evidence_from_reconciliation(
        cost, operations=operations, entries=entries, rate_card_id=card.rate_card_id, card=card
    )


def unavailable_cost(rate_card_id: str) -> CostEvidence:
    return CostEvidence(
        rate_card_id=rate_card_id,
        usage_status="unavailable",
        calculation_status="unavailable",
        reconciliation_status="unavailable",
    )
