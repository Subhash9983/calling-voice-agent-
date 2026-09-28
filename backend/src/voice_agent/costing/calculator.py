"""Decimal cost arithmetic over normalized usage (docs/15 §2, §5; docs/02 §10).

Rules enforced here:

- attempt gross cost = billable quantity x dated unit rate;
- missing usage, rates, or FX are recorded as missing, never priced at zero;
- no intermediate rounding; rounding happens only at the reporting boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from voice_agent.contracts.cost import (
    NORMALIZED_COST_CURRENCY,
    CostCalculation,
    CostLine,
    Currency,
    EvidenceStatus,
    MissingCost,
    MissingCostReason,
    RateCard,
    UnitRate,
)
from voice_agent.contracts.enums import CalculationStatus
from voice_agent.contracts.usage import UsageItem, UsageSource
from voice_agent.ports.costing import MeteredUsage

REPORT_DECIMAL_PLACES = 2
_EVIDENCE_BY_SOURCE = {
    UsageSource.PROVIDER_REPORTED: EvidenceStatus.PROVIDER_USAGE_BASED,
    UsageSource.MEASURED: EvidenceStatus.PROVIDER_USAGE_BASED,
    UsageSource.DERIVED: EvidenceStatus.ESTIMATED,
    UsageSource.ESTIMATED: EvidenceStatus.ESTIMATED,
}


def gross_cost(native_quantity: Decimal, rate: UnitRate) -> tuple[Decimal, Decimal]:
    """Return ``(billable_quantity, gross_cost)`` in the rate's own currency."""
    billable = native_quantity / rate.native_units_per_billing_unit
    return billable, billable / rate.rate_unit_quantity * rate.unit_rate


def round_for_report(amount: Decimal, places: int = REPORT_DECIMAL_PLACES) -> Decimal:
    """Round only at the final reporting boundary (half-up, documented method)."""
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class CostCalculator:
    """Prices metered usage against one immutable rate card."""

    def __init__(self, rate_card: RateCard) -> None:
        self._rate_card = rate_card

    @property
    def rate_card(self) -> RateCard:
        return self._rate_card

    def calculate(
        self,
        usages: Sequence[MeteredUsage],
        *,
        reporting_currency: Currency = NORMALIZED_COST_CURRENCY,
    ) -> CostCalculation:
        lines: list[CostLine] = []
        missing: list[MissingCost] = []
        for metered in usages:
            priced, gaps = self._price_attempt(metered, reporting_currency)
            lines.extend(priced)
            missing.extend(gaps)
        return build_calculation(self._rate_card.rate_card_id, reporting_currency, lines, missing)

    def _price_attempt(
        self, metered: MeteredUsage, reporting_currency: Currency
    ) -> tuple[list[CostLine], list[MissingCost]]:
        if not metered.usage.is_available:
            gap = MissingCost(
                component=metered.component,
                provider=metered.provider,
                model=metered.model,
                reason=MissingCostReason.USAGE_UNAVAILABLE,
            )
            return [], [gap]
        lines: list[CostLine] = []
        missing: list[MissingCost] = []
        for item in metered.usage.items:
            outcome = self._price_item(metered, item, reporting_currency)
            if isinstance(outcome, CostLine):
                lines.append(outcome)
            elif outcome is not None:
                missing.append(outcome)
        return lines, missing

    def _price_item(
        self, metered: MeteredUsage, item: UsageItem, reporting_currency: Currency
    ) -> CostLine | MissingCost | None:
        card = self._rate_card
        if card.is_evidence_only(metered.provider, metered.model, item.unit):
            return None
        rate = card.find_rate(metered.provider, metered.model, item.unit)
        if rate is None:
            return _missing(metered, item, MissingCostReason.RATE_UNAVAILABLE)
        fx = self._rate_card.find_fx(rate.currency, reporting_currency)
        if fx is None:
            return _missing(metered, item, MissingCostReason.FX_UNAVAILABLE)
        billable, gross = gross_cost(item.quantity, rate)
        return CostLine(
            component=metered.component,
            provider=metered.provider,
            model=metered.model,
            usage_unit=item.unit,
            native_quantity=item.quantity,
            billable_quantity=billable,
            billing_unit=rate.billing_unit,
            unit_rate=rate.unit_rate,
            rate_unit_quantity=rate.rate_unit_quantity,
            original_currency=rate.currency,
            gross_cost=gross,
            reporting_currency=reporting_currency,
            fx_rate=fx,
            converted_cost=gross * fx,
            evidence_status=_EVIDENCE_BY_SOURCE[item.source],
            estimated=item.estimated,
        )


def _missing(metered: MeteredUsage, item: UsageItem, reason: MissingCostReason) -> MissingCost:
    return MissingCost(
        component=metered.component,
        provider=metered.provider,
        model=metered.model,
        usage_unit=item.unit,
        reason=reason,
    )


def display_total(
    calculation: CostCalculation, display_currency: Currency, rate_card: RateCard
) -> Decimal | None:
    """Convert a calculation for display from each line's original-currency amount.

    Display only (Decision 069); stored evidence stays normalized. Using the
    original amount means INR charges are never converted through USD.
    Returns ``None`` when nothing was priced or an FX rate is missing.
    """
    if calculation.total is None:
        return None
    total = Decimal(0)
    for line in calculation.lines:
        fx = rate_card.find_fx(line.original_currency, display_currency)
        if fx is None:
            return None
        total += line.gross_cost * fx
    return total


def build_calculation(
    rate_card_id: str,
    reporting_currency: Currency,
    lines: Iterable[CostLine],
    missing: Iterable[MissingCost],
) -> CostCalculation:
    line_tuple = tuple(lines)
    missing_tuple = tuple(missing)
    if not line_tuple:
        status = CalculationStatus.UNAVAILABLE
    elif missing_tuple:
        status = CalculationStatus.PARTIAL
    else:
        status = CalculationStatus.FINAL
    total = sum((line.converted_cost for line in line_tuple), Decimal(0)) if line_tuple else None
    return CostCalculation(
        rate_card_id=rate_card_id,
        reporting_currency=reporting_currency,
        lines=line_tuple,
        missing=missing_tuple,
        status=status,
        total=total,
    )
