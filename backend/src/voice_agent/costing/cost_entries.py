"""Calculation run -> immutable ``cost_entries`` lines (docs/02 §10; docs/15).

Each priced :class:`CostLine` becomes one ``usage_charge`` entry. Derived
quantities and amounts are rounded half-even to the approved 12 fractional
digits at the line total, recorded in ``rounding``. Missing usage/rates
produce no line: unknown cost stays unavailable, never zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from voice_agent.contracts.cost import (
    CostCalculation,
    CostLine,
    EvidenceStatus,
    FxRate,
    RateCard,
)
from voice_agent.contracts.enums import CalculationStatus
from voice_agent.contracts.usage import UsageUnit
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import (
    MAX_DECIMAL_PLACES,
    AggregationBehavior,
    AllocationType,
    CostAmounts,
    CostCategory,
    CostComponent,
    CostEntryRecord,
    CostProviderIdentity,
    CostQuantity,
    CostRate,
    CostReconciliation,
    CostRounding,
    CostScope,
    CurrencyConversion,
    RateSource,
    RateSourceType,
    ReconciliationStatus,
)
from voice_agent.ports.clock import IdGenerator

CALCULATION_ENGINE_VERSION: Final = "phase0_cost_engine_v1"
_QUANTUM: Final = Decimal(1).scaleb(-MAX_DECIMAL_PLACES)


@dataclass(frozen=True, slots=True)
class CostRunContext:
    session_id: str
    calculation_run_id: str
    calculation_version: int
    correlation_id: str
    agent_config_id: str
    environment: AgentConfigEnvironment
    calculated_at: datetime
    rate_source_type: RateSourceType
    rate_source_reference: str
    rate_retrieved_at: datetime
    scope: CostScope = CostScope.SESSION
    turn_id: str | None = None
    operation_id: str | None = None
    # Retry-group reference (docs/02 §10): every attempt line carries it.
    logical_request_id: str | None = None
    supersedes_calculation_run_id: str | None = None


def _round(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN).normalize()


def _utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _fx_evidence(card: RateCard, line: CostLine) -> tuple[str, datetime]:
    if line.original_currency is line.reporting_currency:
        return "same_currency", _utc(card.effective_date)
    match: FxRate | None = next(
        (
            fx
            for fx in card.fx_rates
            if fx.source_currency is line.original_currency
            and fx.target_currency is line.reporting_currency
        ),
        None,
    )
    if match is None:
        return "rate_card_fx", _utc(card.effective_date)
    return match.source_reference, _utc(match.effective_date)


_TOKEN_UNITS: Final = frozenset(
    {
        UsageUnit.INPUT_TOKENS,
        UsageUnit.CACHED_INPUT_TOKENS,
        UsageUnit.CACHE_WRITE_TOKENS,
        UsageUnit.OUTPUT_TOKENS,
        UsageUnit.REASONING_TOKENS,
    }
)


def _method(line: CostLine) -> str:
    if line.estimated:
        if line.usage_unit in _TOKEN_UNITS:
            return "estimated_tokens_x_public_rate"
        return "estimated_quantity_x_public_rate"
    if line.evidence_status is EvidenceStatus.PROVIDER_USAGE_BASED:
        return "provider_reported_quantity_x_public_rate"
    return "measured_audio_x_public_rate"


def cost_entry_for_line(
    line: CostLine, *, card: RateCard, context: CostRunContext, cost_entry_id: str
) -> CostEntryRecord:
    fx_source, fx_effective_at = _fx_evidence(card, line)
    gross = _round(line.gross_cost)
    return CostEntryRecord(
        cost_entry_id=cost_entry_id,
        calculation_run_id=context.calculation_run_id,
        calculation_version=context.calculation_version,
        supersedes_calculation_run_id=context.supersedes_calculation_run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        operation_id=context.operation_id,
        logical_request_id=context.logical_request_id,
        correlation_id=context.correlation_id,
        agent_config_id=context.agent_config_id,
        component=CostComponent(line.component.value),
        cost_category=CostCategory.USAGE_CHARGE,
        scope=context.scope,
        provider_identity=CostProviderIdentity(provider=line.provider, model=line.model),
        quantity=CostQuantity(
            native_quantity=_round(line.native_quantity),
            native_unit=line.usage_unit.value,
            billable_quantity=_round(line.billable_quantity),
            billing_unit=line.billing_unit,
            conversion_method="rate_card_native_units_per_billing_unit",
        ),
        rate=CostRate(
            unit_rate=line.unit_rate,
            rate_unit_quantity=line.rate_unit_quantity,
            currency=line.original_currency,
            pricing_basis=line.pricing_basis,
            rate_effective_from=_utc(card.effective_date),
            rate_card_version=card.rate_card_id,
        ),
        rate_source=RateSource(
            source_type=context.rate_source_type,
            source_reference=context.rate_source_reference,
            retrieved_at=context.rate_retrieved_at,
            effective_date=_utc(card.effective_date),
        ),
        amounts=CostAmounts(
            gross_cost=gross,
            discount_amount=Decimal(0),
            net_cost_original_currency=gross,
            tax_treatment="excluded",
        ),
        currency_conversion=CurrencyConversion(
            original_currency=line.original_currency,
            reporting_currency=line.reporting_currency,
            fx_rate=line.fx_rate,
            fx_source=fx_source,
            fx_effective_at=fx_effective_at,
            converted_net_cost=_round(line.converted_cost),
        ),
        rounding=CostRounding(
            rounding_mode="half_even",
            decimal_places=MAX_DECIMAL_PLACES,
            rounding_applied_at="line_total",
            minimum_charge_applied=False,
        ),
        evidence_status=line.evidence_status,
        calculation_status=CalculationStatus.FINAL,
        reconciliation=CostReconciliation(status=ReconciliationStatus.NOT_CHECKED),
        allocation_type=AllocationType.DIRECT,
        aggregation_behavior=AggregationBehavior.CHARGE,
        calculation_method=_method(line),
        calculation_engine_version=CALCULATION_ENGINE_VERSION,
        calculated_at=context.calculated_at,
        environment=context.environment,
    )


def cost_entries_from_calculation(
    calculation: CostCalculation,
    *,
    card: RateCard,
    context: CostRunContext,
    ids: IdGenerator,
) -> tuple[CostEntryRecord, ...]:
    """One entry per priced line; a partial run records its lines as ``partial``."""
    entries: Sequence[CostEntryRecord] = [
        cost_entry_for_line(line, card=card, context=context, cost_entry_id=ids.new_id())
        for line in calculation.lines
    ]
    status = calculation.status
    return tuple(entry.model_copy(update={"calculation_status": status}) for entry in entries)
