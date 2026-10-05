"""Cost contracts: dated rates, FX evidence, and calculated lines (docs/02 §10, docs/15).

All quantities, rates, FX values, and money are ``Decimal``; binary floats are
never used. A missing rate or usage quantity is recorded as missing, never
as zero cost.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from voice_agent.contracts.base import (
    ExternalIdentifier,
    FiniteDecimal,
    FiniteNonNegativeDecimal,
    NonNegativeDecimal,
    PositiveDecimal,
    ShortLabel,
    StrictModel,
)
from voice_agent.contracts.enums import CalculationStatus, OperationComponent
from voice_agent.contracts.usage import UsageUnit


class Currency(StrEnum):
    USD = "USD"
    INR = "INR"


# Decision 016 / Decision 069: stored cost evidence normalizes to USD; INR is a
# display/report conversion only.
NORMALIZED_COST_CURRENCY = Currency.USD


class PricingBasis(StrEnum):
    PER_UNIT = "per_unit"
    TIERED = "tiered"
    FLAT = "flat"
    INCLUDED_ALLOWANCE = "included_allowance"
    NEGOTIATED = "negotiated"


class FxType(StrEnum):
    PLANNING = "planning"
    REFERENCE = "reference"
    ACTUAL = "actual"


class EvidenceStatus(StrEnum):
    ESTIMATED = "estimated"
    PROVIDER_USAGE_BASED = "provider_usage_based"
    PROVIDER_COST_REPORTED = "provider_cost_reported"
    INVOICE_RECONCILED = "invoice_reconciled"
    MANUAL = "manual"


class MissingCostReason(StrEnum):
    USAGE_UNAVAILABLE = "usage_unavailable"
    RATE_UNAVAILABLE = "rate_unavailable"
    FX_UNAVAILABLE = "fx_unavailable"


class UnitRate(StrictModel):
    """One dated billable meter.

    ``gross = native_quantity / native_units_per_billing_unit
    / rate_unit_quantity * unit_rate`` (docs/02 §10 embedded ``rate``).
    """

    provider: ShortLabel
    model: ExternalIdentifier | None = None
    usage_unit: UsageUnit
    billing_unit: ShortLabel
    native_units_per_billing_unit: PositiveDecimal = Decimal(1)
    unit_rate: NonNegativeDecimal
    rate_unit_quantity: PositiveDecimal
    currency: Currency
    pricing_basis: PricingBasis = PricingBasis.PER_UNIT


class FxRate(StrictModel):
    source_currency: Currency
    target_currency: Currency
    rate: PositiveDecimal
    fx_type: FxType
    source_reference: Annotated[str, Field(min_length=1, max_length=256)]
    effective_date: date


class MeterKey(StrictModel):
    """Identifies a usage unit that is recorded as evidence but is not a billing meter."""

    provider: ShortLabel
    model: ExternalIdentifier | None = None
    usage_unit: UsageUnit


class RateCard(StrictModel):
    rate_card_id: ShortLabel
    effective_date: date
    rates: tuple[UnitRate, ...]
    fx_rates: tuple[FxRate, ...] = ()
    evidence_only: tuple[MeterKey, ...] = ()

    @model_validator(mode="after")
    def _unique_meters(self) -> RateCard:
        keys = [(r.provider, r.model, r.usage_unit) for r in self.rates]
        if len(keys) != len(set(keys)):
            raise ValueError("a rate card cannot define the same meter twice")
        return self

    def find_rate(self, provider: str, model: str | None, unit: UsageUnit) -> UnitRate | None:
        for rate in self.rates:
            if rate.provider == provider and rate.model == model and rate.usage_unit is unit:
                return rate
        return None

    def is_evidence_only(self, provider: str, model: str | None, unit: UsageUnit) -> bool:
        return any(
            key.provider == provider and key.model == model and key.usage_unit is unit
            for key in self.evidence_only
        )

    def find_fx(self, source: Currency, target: Currency) -> Decimal | None:
        if source is target:
            return Decimal(1)
        for fx in self.fx_rates:
            if fx.source_currency is source and fx.target_currency is target:
                return fx.rate
        return None


class CostLine(StrictModel):
    component: OperationComponent
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    usage_unit: UsageUnit
    native_quantity: NonNegativeDecimal
    billable_quantity: FiniteNonNegativeDecimal
    billing_unit: ShortLabel
    unit_rate: NonNegativeDecimal
    rate_unit_quantity: PositiveDecimal
    original_currency: Currency
    gross_cost: FiniteDecimal
    reporting_currency: Currency
    fx_rate: PositiveDecimal
    converted_cost: FiniteDecimal
    evidence_status: EvidenceStatus
    estimated: bool
    # The dated meter's basis, e.g. LiveKit ``included_allowance`` (a known zero).
    pricing_basis: PricingBasis = PricingBasis.PER_UNIT


class MissingCost(StrictModel):
    component: OperationComponent
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    usage_unit: UsageUnit | None = None
    reason: MissingCostReason


class CostCalculation(StrictModel):
    """Result of one calculation run; ``total`` is ``None`` when nothing is priced."""

    rate_card_id: ShortLabel
    reporting_currency: Currency
    lines: tuple[CostLine, ...] = ()
    missing: tuple[MissingCost, ...] = ()
    status: CalculationStatus
    total: FiniteDecimal | None = None

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> CostCalculation:
        expected = _status_for(self.lines, self.missing)
        if self.status is not expected:
            raise ValueError(f"calculation status must be {expected.value}")
        if (self.total is None) != (not self.lines):
            raise ValueError("total is present exactly when at least one line is priced")
        return self


def _status_for(lines: tuple[CostLine, ...], missing: tuple[MissingCost, ...]) -> CalculationStatus:
    if not lines:
        return CalculationStatus.UNAVAILABLE
    return CalculationStatus.PARTIAL if missing else CalculationStatus.FINAL
