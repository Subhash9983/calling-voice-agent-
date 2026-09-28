"""Usage normalization: provider counts -> normalized units; missing never becomes zero."""

from __future__ import annotations

from decimal import Decimal

import pytest

from voice_agent.contracts.usage import UsageReport, UsageReportingStatus, UsageSource, UsageUnit
from voice_agent.costing.usage_normalization import (
    UsageNormalizationError,
    llm_usage,
    merge_reports,
    stt_usage,
    tts_usage,
)


def test_llm_total_input_is_split_into_uncached_and_cached() -> None:
    usage = llm_usage(
        total_input_tokens=1200, cached_input_tokens=200, output_tokens=250, reasoning_tokens=0
    )

    assert usage.reporting_status is UsageReportingStatus.PROVIDER_REPORTED
    assert usage.quantity_of(UsageUnit.INPUT_TOKENS) == 1000
    assert usage.quantity_of(UsageUnit.CACHED_INPUT_TOKENS) == 200
    assert usage.quantity_of(UsageUnit.OUTPUT_TOKENS) == 250
    assert usage.quantity_of(UsageUnit.REASONING_TOKENS) == 0


def test_missing_llm_values_stay_unavailable() -> None:
    partial = llm_usage(total_input_tokens=None, cached_input_tokens=None, output_tokens=40)
    none = llm_usage(total_input_tokens=None, cached_input_tokens=None, output_tokens=None)

    assert partial.quantity_of(UsageUnit.INPUT_TOKENS) is None
    assert partial.quantity_of(UsageUnit.OUTPUT_TOKENS) == 40
    assert none == UsageReport.unavailable()


def test_cached_tokens_cannot_exceed_total() -> None:
    with pytest.raises(UsageNormalizationError):
        llm_usage(total_input_tokens=10, cached_input_tokens=11, output_tokens=0)


def test_stt_milliseconds_become_exact_seconds() -> None:
    usage = stt_usage(
        transcribed_audio_ms=1234, connected_audio_ms=5000, source=UsageSource.MEASURED
    )

    assert usage.reporting_status is UsageReportingStatus.MEASURED
    assert usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal("1.234")
    assert usage.quantity_of(UsageUnit.CONNECTED_AUDIO_SECONDS) == Decimal(5)
    assert stt_usage(transcribed_audio_ms=None) == UsageReport.unavailable()


def test_tts_characters_and_estimates_are_labelled() -> None:
    usage = tts_usage(
        synthesized_characters=42, generated_audio_ms=900, source=UsageSource.ESTIMATED
    )

    assert usage.reporting_status is UsageReportingStatus.ESTIMATED
    assert all(item.estimated for item in usage.items)
    assert usage.quantity_of(UsageUnit.GENERATED_AUDIO_SECONDS) == Decimal("0.9")


def test_merge_sums_by_unit_and_ignores_unavailable_reports() -> None:
    merged = merge_reports(
        [
            stt_usage(transcribed_audio_ms=1000),
            UsageReport.unavailable(),
            stt_usage(transcribed_audio_ms=500),
        ]
    )

    assert merged.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal("1.5")
    assert merged.reporting_status is UsageReportingStatus.PROVIDER_REPORTED


def test_merge_of_nothing_is_unavailable_and_estimates_propagate() -> None:
    assert merge_reports([]) == UsageReport.unavailable()
    merged = merge_reports(
        [
            tts_usage(synthesized_characters=10, source=UsageSource.MEASURED),
            tts_usage(synthesized_characters=5, source=UsageSource.ESTIMATED),
        ]
    )
    assert merged.reporting_status is UsageReportingStatus.ESTIMATED
    assert merged.items[0].estimated
    measured_only = merge_reports(
        [tts_usage(synthesized_characters=1, source=UsageSource.MEASURED)]
    )
    assert measured_only.reporting_status is UsageReportingStatus.MEASURED


def test_cache_write_tokens_are_a_separate_meter_and_absent_means_unavailable() -> None:
    reported = llm_usage(
        total_input_tokens=1000,
        cached_input_tokens=0,
        cache_write_tokens=400,
        output_tokens=10,
    )
    absent = llm_usage(total_input_tokens=1000, cached_input_tokens=0, output_tokens=10)

    assert reported.quantity_of(UsageUnit.CACHE_WRITE_TOKENS) == 400
    assert reported.quantity_of(UsageUnit.INPUT_TOKENS) == 1000
    assert absent.quantity_of(UsageUnit.CACHE_WRITE_TOKENS) is None
