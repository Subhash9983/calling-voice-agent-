"""Decimal cost arithmetic fixtures from docs/15 §6-§8A (Decision 039/067 S13)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from voice_agent.contracts.cost import NORMALIZED_COST_CURRENCY, Currency, MissingCostReason
from voice_agent.contracts.enums import CalculationStatus, OperationComponent
from voice_agent.contracts.usage import UsageReport, UsageUnit
from voice_agent.costing.budget import (
    ABSOLUTE_MONTHLY_CEILING_INR,
    exceeds_ceiling,
    per_minute,
    planning_budget,
)
from voice_agent.costing.calculator import CostCalculator, display_total, round_for_report
from voice_agent.costing.rate_card import (
    DEEPGRAM,
    OPENAI,
    PHASE0_RATE_CARD_ID,
    SARVAM,
    phase0_promotional_rate_card,
    phase0_rate_card,
)
from voice_agent.costing.usage_normalization import llm_usage, stt_usage, tts_usage
from voice_agent.ports.costing import MeteredUsage

TEN_MINUTES_MS = 10 * 60 * 1000


def _planning_session() -> list[MeteredUsage]:
    """docs/15 §6: 10 STT minutes, 60,000 input + 2,500 output tokens, 6,000 characters."""
    return [
        MeteredUsage(
            OperationComponent.STT, *DEEPGRAM, stt_usage(transcribed_audio_ms=TEN_MINUTES_MS)
        ),
        MeteredUsage(
            OperationComponent.CONVERSATION_ENGINE,
            *OPENAI,
            llm_usage(total_input_tokens=60_000, cached_input_tokens=None, output_tokens=2_500),
        ),
        MeteredUsage(
            OperationComponent.TTS,
            *SARVAM,
            tts_usage(synthesized_characters=6_000, generated_audio_ms=95_000),
        ),
    ]


def _by_unit(calculation_lines: object) -> dict[UsageUnit, Decimal]:
    return {line.usage_unit: line.converted_cost for line in calculation_lines}  # type: ignore[attr-defined]


def test_conservative_ten_minute_session_is_inr_27_925() -> None:
    calculation = CostCalculator(phase0_rate_card()).calculate(
        _planning_session(), reporting_currency=Currency.INR
    )

    costs = _by_unit(calculation.lines)
    assert costs[UsageUnit.TRANSCRIBED_AUDIO_SECONDS] == Decimal("9.20")
    assert costs[UsageUnit.INPUT_TOKENS] == Decimal("0.60")
    assert costs[UsageUnit.OUTPUT_TOKENS] == Decimal("0.125")
    assert costs[UsageUnit.SYNTHESIZED_CHARACTERS] == Decimal("18.00")
    assert calculation.total == Decimal("27.925")
    assert calculation.status is CalculationStatus.FINAL
    assert calculation.rate_card_id == PHASE0_RATE_CARD_ID
    assert round_for_report(calculation.total) == Decimal("27.93")
    assert per_minute(calculation.total, Decimal(10)) == Decimal("2.7925")


def test_sarvam_inr_is_not_converted_through_usd() -> None:
    calculation = CostCalculator(phase0_rate_card()).calculate(
        _planning_session(), reporting_currency=Currency.INR
    )

    sarvam = next(line for line in calculation.lines if line.provider == SARVAM[0])
    assert sarvam.original_currency is Currency.INR
    assert sarvam.fx_rate == 1
    assert sarvam.gross_cost == Decimal("18.00")


def test_usd_reporting_preserves_original_currency_evidence() -> None:
    calculation = CostCalculator(phase0_rate_card()).calculate(
        _planning_session(), reporting_currency=Currency.USD
    )

    deepgram = next(line for line in calculation.lines if line.provider == DEEPGRAM[0])
    assert deepgram.gross_cost == Decimal("0.092")
    assert deepgram.billable_quantity == Decimal(10)
    assert calculation.total == Decimal("0.27925")


def test_promotional_deepgram_variant_is_inr_24_525() -> None:
    calculation = CostCalculator(phase0_promotional_rate_card()).calculate(
        _planning_session(), reporting_currency=Currency.INR
    )

    assert calculation.total == Decimal("24.525")


@pytest.mark.parametrize(
    ("input_tokens", "expected_inr"),
    [(20_000, Decimal("0.20")), (60_000, Decimal("0.60")), (120_000, Decimal("1.20"))],
)
def test_llm_input_sensitivity(input_tokens: int, expected_inr: Decimal) -> None:
    usage = [
        MeteredUsage(
            OperationComponent.CONVERSATION_ENGINE,
            *OPENAI,
            llm_usage(
                total_input_tokens=input_tokens, cached_input_tokens=None, output_tokens=None
            ),
        )
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usage, reporting_currency=Currency.INR
    )

    assert calculation.total == expected_inr


def test_cached_input_is_priced_separately_without_double_counting() -> None:
    usage = [
        MeteredUsage(
            OperationComponent.CONVERSATION_ENGINE,
            *OPENAI,
            llm_usage(total_input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=0),
        )
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usage, reporting_currency=Currency.USD
    )

    costs = _by_unit(calculation.lines)
    assert costs[UsageUnit.INPUT_TOKENS] == Decimal("0.06")
    assert costs[UsageUnit.CACHED_INPUT_TOKENS] == Decimal("0.004")


def test_full_evaluation_transcript_layer_projection_is_inr_6_75() -> None:
    per_response = llm_usage(total_input_tokens=2_500, cached_input_tokens=None, output_tokens=250)
    usages = [
        MeteredUsage(OperationComponent.CONVERSATION_ENGINE, *OPENAI, per_response)
        for _ in range(180)
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usages, reporting_currency=Currency.INR
    )

    assert calculation.total == Decimal("6.75")


def test_unavailable_usage_is_missing_never_zero() -> None:
    usages = [MeteredUsage(OperationComponent.TTS, *SARVAM, UsageReport.unavailable())]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usages, reporting_currency=Currency.INR
    )

    assert calculation.status is CalculationStatus.UNAVAILABLE
    assert calculation.total is None
    assert calculation.missing[0].reason is MissingCostReason.USAGE_UNAVAILABLE


def test_unknown_rate_makes_the_calculation_partial() -> None:
    usages = [
        MeteredUsage(OperationComponent.STT, *DEEPGRAM, stt_usage(transcribed_audio_ms=60_000)),
        MeteredUsage(
            OperationComponent.TTS, "elevenlabs", "flash_v2_5", tts_usage(synthesized_characters=10)
        ),
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usages, reporting_currency=Currency.INR
    )

    assert calculation.status is CalculationStatus.PARTIAL
    assert calculation.missing[0].reason is MissingCostReason.RATE_UNAVAILABLE
    assert calculation.total == Decimal("0.92")


def test_evidence_only_meters_are_neither_priced_nor_missing() -> None:
    usages = [
        MeteredUsage(
            OperationComponent.TTS,
            *SARVAM,
            tts_usage(synthesized_characters=1000, generated_audio_ms=4_000),
        )
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usages, reporting_currency=Currency.INR
    )

    assert calculation.status is CalculationStatus.FINAL
    assert [line.usage_unit for line in calculation.lines] == [UsageUnit.SYNTHESIZED_CHARACTERS]


def test_twenty_session_expected_budget_matches_doc_table() -> None:
    budget = planning_budget(
        variable_per_session=Decimal("27.925"), sessions=20, fixed_platform_cost=Decimal(800)
    ).rounded()

    assert budget.variable_total == Decimal("558.50")
    assert budget.pre_tax_subtotal == Decimal("1358.50")
    assert budget.after_contingency == Decimal("1630.20")
    assert budget.after_tax_buffer == Decimal("1923.64")


def test_atlas_maximum_budget_matches_doc_table_and_stays_under_ceiling() -> None:
    budget = planning_budget(
        variable_per_session=Decimal("27.925"), sessions=20, fixed_platform_cost=Decimal(3000)
    ).rounded()

    assert budget.pre_tax_subtotal == Decimal("3558.50")
    assert budget.after_contingency == Decimal("4270.20")
    assert budget.after_tax_buffer == Decimal("5038.84")
    assert not exceeds_ceiling(budget.after_tax_buffer)
    assert exceeds_ceiling(ABSOLUTE_MONTHLY_CEILING_INR + Decimal("0.01"))


def test_budget_rejects_negative_sessions_and_zero_minutes() -> None:
    with pytest.raises(ValueError, match="negative"):
        planning_budget(
            variable_per_session=Decimal(1), sessions=-1, fixed_platform_cost=Decimal(0)
        )
    with pytest.raises(ValueError, match="positive"):
        per_minute(Decimal(1), Decimal(0))


def test_no_float_enters_cost_lines() -> None:
    calculation = CostCalculator(phase0_rate_card()).calculate(
        _planning_session(), reporting_currency=Currency.INR
    )

    for line in calculation.lines:
        for value in (line.gross_cost, line.converted_cost, line.billable_quantity, line.unit_rate):
            assert isinstance(value, Decimal)


def test_cache_write_tokens_follow_the_docs_15_formula() -> None:
    """docs/15 §2.4: uncached x 0.10 + cached x 0.01 + cache-write x 0.125 + output x 0.50."""
    usage = [
        MeteredUsage(
            OperationComponent.CONVERSATION_ENGINE,
            *OPENAI,
            llm_usage(
                total_input_tokens=1_000_000,
                cached_input_tokens=200_000,
                cache_write_tokens=100_000,
                output_tokens=10_000,
            ),
        )
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usage, reporting_currency=Currency.USD
    )

    costs = _by_unit(calculation.lines)
    assert costs[UsageUnit.CACHE_WRITE_TOKENS] == Decimal("0.0125")
    assert calculation.total == Decimal("0.08") + Decimal("0.002") + Decimal("0.0125") + Decimal(
        "0.005"
    )
    assert calculation.status is CalculationStatus.FINAL


def test_unreported_cache_writes_are_not_priced_as_zero() -> None:
    usage = [
        MeteredUsage(
            OperationComponent.CONVERSATION_ENGINE,
            *OPENAI,
            llm_usage(total_input_tokens=10, cached_input_tokens=None, output_tokens=None),
        )
    ]

    calculation = CostCalculator(phase0_rate_card()).calculate(
        usage, reporting_currency=Currency.USD
    )

    assert UsageUnit.CACHE_WRITE_TOKENS not in _by_unit(calculation.lines)


def test_normalized_currency_is_usd_and_inr_is_a_display_conversion() -> None:
    """Decision 069: USD normalized (Decision 016); INR 27.925 is a display figure."""
    calculator = CostCalculator(phase0_rate_card())

    calculation = calculator.calculate(_planning_session())

    assert NORMALIZED_COST_CURRENCY is Currency.USD
    assert calculation.reporting_currency is Currency.USD
    assert calculation.total == Decimal("0.27925")
    assert display_total(calculation, Currency.INR, phase0_rate_card()) == Decimal("27.925")
    assert display_total(calculation, Currency.USD, phase0_rate_card()) == Decimal("0.27925")


def test_display_conversion_uses_original_currency_amounts() -> None:
    calculation = CostCalculator(phase0_rate_card()).calculate(_planning_session())
    sarvam = next(line for line in calculation.lines if line.provider == SARVAM[0])
    only_sarvam = calculation.model_copy(
        update={"lines": (sarvam,), "total": sarvam.converted_cost}
    )

    assert display_total(only_sarvam, Currency.INR, phase0_rate_card()) == Decimal("18.00")


def test_display_of_unavailable_calculation_is_none() -> None:
    usages = [MeteredUsage(OperationComponent.TTS, *SARVAM, UsageReport.unavailable())]
    calculation = CostCalculator(phase0_rate_card()).calculate(usages)

    assert display_total(calculation, Currency.INR, phase0_rate_card()) is None
