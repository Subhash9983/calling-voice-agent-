"""Reproducible component-level cost evidence line (docs/02 §10).

Calculation runs are immutable: a recalculation writes a new run with a
higher ``calculation_version``. ``allocation_only`` lines never contribute to
the session total. Every money/quantity/rate/FX value is ``Decimal``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    PositiveDecimal,
    PreciseDecimal,
    ShortLabel,
    UtcDatetime,
)
from voice_agent.contracts.cost import Currency, EvidenceStatus, PricingBasis
from voice_agent.contracts.enums import CalculationStatus
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.records_common import (
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    SafeNote,
    Visibility,
)

COST_ENTRY_SCHEMA_VERSION: Final = 1
MAX_DECIMAL_PLACES = 12


class CostComponent(StrEnum):
    TRANSPORT = "transport"
    STT = "stt"
    CONVERSATION_ENGINE = "conversation_engine"
    TTS = "tts"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    RECORDING = "recording"


class CostCategory(StrEnum):
    USAGE_CHARGE = "usage_charge"
    PLATFORM_FEE = "platform_fee"
    RECORDING_CHARGE = "recording_charge"
    TELEPHONY_CHARGE = "telephony_charge"
    MINIMUM_CHARGE = "minimum_charge"
    DISCOUNT = "discount"
    CREDIT = "credit"
    TAX = "tax"
    ADJUSTMENT = "adjustment"


class CostScope(StrEnum):
    SESSION = "session"
    TURN = "turn"
    OPERATION = "operation"


class RateSourceType(StrEnum):
    PUBLIC_PRICE = "public_price"
    PROVIDER_DOCUMENTATION = "provider_documentation"
    CONTRACT = "contract"
    PROVIDER_INVOICE = "provider_invoice"
    MANUAL_OVERRIDE = "manual_override"


class AllocationType(StrEnum):
    DIRECT = "direct"
    SESSION_PRORATED = "session_prorated"
    SHARED = "shared"
    UNALLOCATED = "unallocated"


class AggregationBehavior(StrEnum):
    CHARGE = "charge"
    ALLOCATION_ONLY = "allocation_only"


class ReconciliationStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    MATCHED = "matched"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


class CostProviderIdentity(RecordModel):
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    region_label: ShortLabel | None = None
    provider_sku: ExternalIdentifier | None = None
    pricing_tier: ShortLabel | None = None
    provider_request_id: ExternalIdentifier | None = None


class CostQuantity(RecordModel):
    native_quantity: PreciseDecimal
    native_unit: ShortLabel
    billable_quantity: PreciseDecimal
    billing_unit: ShortLabel
    conversion_factor: PreciseDecimal | None = None
    conversion_method: ShortLabel


class CostRate(RecordModel):
    unit_rate: PreciseDecimal
    rate_unit_quantity: PositiveDecimal
    currency: Currency
    pricing_basis: PricingBasis
    tier_from: PreciseDecimal | None = None
    tier_to: PreciseDecimal | None = None
    minimum_billable_quantity: PreciseDecimal | None = None
    billing_increment: PreciseDecimal | None = None
    rate_effective_from: UtcDatetime
    rate_effective_to: UtcDatetime | None = None
    rate_card_version: ExternalIdentifier


class RateSource(RecordModel):
    source_type: RateSourceType
    source_reference: ExternalIdentifier
    retrieved_at: UtcDatetime
    effective_date: UtcDatetime
    verified_by: ExternalIdentifier | None = None
    evidence_note: SafeNote | None = None


class CostAmounts(RecordModel):
    gross_cost: PreciseDecimal
    discount_amount: PreciseDecimal
    net_cost_original_currency: PreciseDecimal
    credit_amount: PreciseDecimal | None = None
    tax_amount: PreciseDecimal | None = None
    tax_treatment: Literal["excluded", "included", "not_applicable", "unknown"]


class CurrencyConversion(RecordModel):
    original_currency: Currency
    reporting_currency: Currency
    fx_rate: PositiveDecimal
    fx_source: ExternalIdentifier
    fx_effective_at: UtcDatetime
    converted_net_cost: PreciseDecimal


class CostRounding(RecordModel):
    rounding_mode: ShortLabel
    decimal_places: Annotated[NonNegativeInt, Field(le=MAX_DECIMAL_PLACES)]
    rounding_applied_at: Literal["quantity", "line_total", "invoice_only"]
    billing_increment: PreciseDecimal | None = None
    minimum_charge_applied: bool


class CostReconciliation(RecordModel):
    status: ReconciliationStatus
    provider_reported_cost: PreciseDecimal | None = None
    difference_amount: PreciseDecimal | None = None
    difference_percent: PreciseDecimal | None = None
    checked_at: UtcDatetime | None = None
    note: SafeNote | None = None


class CostEntryRecord(RecordModel):
    cost_entry_id: CanonicalId
    calculation_run_id: CanonicalId
    calculation_version: PositiveInt
    supersedes_calculation_run_id: CanonicalId | None = None
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    logical_request_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    agent_config_id: CanonicalId
    schema_version: Literal[1] = COST_ENTRY_SCHEMA_VERSION
    component: CostComponent
    cost_category: CostCategory
    scope: CostScope
    provider_identity: CostProviderIdentity
    quantity: CostQuantity
    rate: CostRate
    rate_source: RateSource
    amounts: CostAmounts
    currency_conversion: CurrencyConversion
    rounding: CostRounding
    evidence_status: EvidenceStatus
    calculation_status: CalculationStatus
    reconciliation: CostReconciliation
    allocation_type: AllocationType
    aggregation_behavior: AggregationBehavior
    source_cost_entry_id: CanonicalId | None = None
    allocation_basis: Literal["duration", "audio_seconds", "turn_count", "token_count"] | None = (
        None
    )
    allocation_ratio: PreciseDecimal | None = None
    calculation_method: ShortLabel
    calculation_engine_version: ShortLabel
    calculated_at: UtcDatetime
    environment: AgentConfigEnvironment
    calculated_by: ExternalIdentifier | None = None
    notes: SafeNote | None = None
    expires_at: UtcDatetime | None = None
    visibility: Visibility = Visibility.INTERNAL

    @model_validator(mode="after")
    def _scope_target(self) -> CostEntryRecord:
        if self.scope is CostScope.TURN and self.turn_id is None:
            raise ValueError("a turn-scope cost entry requires turn_id")
        if self.scope is CostScope.OPERATION and self.operation_id is None:
            raise ValueError("an operation-scope cost entry requires operation_id")
        return self

    @property
    def scope_target_id(self) -> str:
        if self.scope is CostScope.TURN and self.turn_id is not None:
            return self.turn_id
        if self.scope is CostScope.OPERATION and self.operation_id is not None:
            return self.operation_id
        return self.session_id
