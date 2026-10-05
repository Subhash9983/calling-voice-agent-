"""The single centralized Phase 0 rate card (docs/15 §2-§3, Decision 039/067 S13).

Pricing definitions live only here; provider and orchestration modules never
hold price constants.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from voice_agent.contracts.cost import (
    Currency,
    FxRate,
    FxType,
    MeterKey,
    PricingBasis,
    RateCard,
    UnitRate,
)
from voice_agent.contracts.usage import UsageUnit

PHASE0_RATE_CARD_ID = "phase0_rate_card_2026_09_26_v1"
PHASE0_PROMO_RATE_CARD_ID = "phase0_rate_card_2026_09_26_v1_deepgram_promo"
RATE_CARD_DATE = date(2026, 9, 26)

DEEPGRAM = ("deepgram", "nova-3")
OPENAI = ("openai", "gpt-6-luna")
SARVAM = ("sarvam", "bulbul:v3")
LIVEKIT = ("livekit", None)

SECONDS_PER_MINUTE = Decimal(60)
ONE = Decimal(1)
PER_MILLION = Decimal(1_000_000)
PER_THOUSAND = Decimal(1_000)

DEEPGRAM_REGULAR_USD_PER_MINUTE = Decimal("0.0092")
DEEPGRAM_PROMO_USD_PER_MINUTE = Decimal("0.0058")

PLANNING_FX = FxRate(
    source_currency=Currency.USD,
    target_currency=Currency.INR,
    rate=Decimal(100),
    fx_type=FxType.PLANNING,
    source_reference="docs/15 §3 approved conservative planning rate",
    effective_date=RATE_CARD_DATE,
)
PLANNING_FX_INR_TO_USD = FxRate(
    source_currency=Currency.INR,
    target_currency=Currency.USD,
    rate=Decimal("0.01"),
    fx_type=FxType.PLANNING,
    source_reference="inverse of the docs/15 §3 planning rate",
    effective_date=RATE_CARD_DATE,
)
# Recorded as evidence only; planning never uses it (docs/15 §3).
RBI_REFERENCE_FX = FxRate(
    source_currency=Currency.USD,
    target_currency=Currency.INR,
    rate=Decimal("95.9099"),
    fx_type=FxType.REFERENCE,
    source_reference="RBI/FBIL reference archive at MSEI",
    effective_date=date(2026, 9, 24),
)


def _deepgram(usd_per_minute: Decimal) -> UnitRate:
    return UnitRate(
        provider=DEEPGRAM[0],
        model=DEEPGRAM[1],
        usage_unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
        billing_unit="minute",
        native_units_per_billing_unit=SECONDS_PER_MINUTE,
        unit_rate=usd_per_minute,
        rate_unit_quantity=ONE,
        currency=Currency.USD,
    )


def _openai(unit: UsageUnit, usd_per_million: str) -> UnitRate:
    return UnitRate(
        provider=OPENAI[0],
        model=OPENAI[1],
        usage_unit=unit,
        billing_unit="token",
        unit_rate=Decimal(usd_per_million),
        rate_unit_quantity=PER_MILLION,
        currency=Currency.USD,
    )


def _shared_rates() -> tuple[UnitRate, ...]:
    return (
        _openai(UsageUnit.INPUT_TOKENS, "0.10"),
        _openai(UsageUnit.CACHED_INPUT_TOKENS, "0.01"),
        _openai(UsageUnit.CACHE_WRITE_TOKENS, "0.125"),
        _openai(UsageUnit.OUTPUT_TOKENS, "0.50"),
        UnitRate(
            provider=SARVAM[0],
            model=SARVAM[1],
            usage_unit=UsageUnit.SYNTHESIZED_CHARACTERS,
            billing_unit="character",
            unit_rate=Decimal("3.00"),
            rate_unit_quantity=PER_THOUSAND,
            currency=Currency.INR,
        ),
        UnitRate(
            provider=LIVEKIT[0],
            model=LIVEKIT[1],
            usage_unit=UsageUnit.TRANSPORT_SESSION_SECONDS,
            billing_unit="participant_minute",
            native_units_per_billing_unit=SECONDS_PER_MINUTE,
            unit_rate=Decimal(0),
            rate_unit_quantity=ONE,
            currency=Currency.USD,
            pricing_basis=PricingBasis.INCLUDED_ALLOWANCE,
        ),
    )


# Reported for evidence but not billed separately (docs/15 §2.3-§2.5): Sarvam
# bills accepted characters, not audio duration; Deepgram is budgeted on the
# transcribed stream duration; OpenAI output tokens already include reasoning.
_EVIDENCE_ONLY: tuple[MeterKey, ...] = (
    MeterKey(provider=SARVAM[0], model=SARVAM[1], usage_unit=UsageUnit.GENERATED_AUDIO_SECONDS),
    MeterKey(provider=DEEPGRAM[0], model=DEEPGRAM[1], usage_unit=UsageUnit.CONNECTED_AUDIO_SECONDS),
    MeterKey(provider=OPENAI[0], model=OPENAI[1], usage_unit=UsageUnit.REASONING_TOKENS),
)


def phase0_rate_card() -> RateCard:
    """Budget rate card: Deepgram at the regular rate (docs/15 §2.3)."""
    return RateCard(
        rate_card_id=PHASE0_RATE_CARD_ID,
        effective_date=RATE_CARD_DATE,
        rates=(_deepgram(DEEPGRAM_REGULAR_USD_PER_MINUTE), *_shared_rates()),
        fx_rates=(PLANNING_FX, PLANNING_FX_INR_TO_USD),
        evidence_only=_EVIDENCE_ONLY,
    )


def phase0_promotional_rate_card() -> RateCard:
    """Actual-estimate variant at the displayed promotional Deepgram rate."""
    return RateCard(
        rate_card_id=PHASE0_PROMO_RATE_CARD_ID,
        effective_date=RATE_CARD_DATE,
        rates=(_deepgram(DEEPGRAM_PROMO_USD_PER_MINUTE), *_shared_rates()),
        fx_rates=(PLANNING_FX, PLANNING_FX_INR_TO_USD),
        evidence_only=_EVIDENCE_ONLY,
    )


# Every approved, dated version. A rate change adds a new ID here; an existing
# entry is never edited, so historical evidence keeps its own rates (docs/15 §13).
_REGISTRY: Final[Mapping[str, Callable[[], RateCard]]] = MappingProxyType(
    {
        PHASE0_RATE_CARD_ID: phase0_rate_card,
        PHASE0_PROMO_RATE_CARD_ID: phase0_promotional_rate_card,
    }
)
KNOWN_RATE_CARD_IDS: Final = tuple(_REGISTRY)


def rate_card_by_id(rate_card_id: str) -> RateCard | None:
    """The exact approved card for a stored ``rate_card_version``; ``None`` if unknown."""
    factory = _REGISTRY.get(rate_card_id)
    return None if factory is None else factory()


class ApprovedRateCards:
    """``RateLookup`` port over the approved registry (exact version only)."""

    def rate_card(self, rate_card_id: str) -> RateCard | None:
        return rate_card_by_id(rate_card_id)
