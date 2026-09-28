"""Normalize provider-native usage counts into :class:`UsageReport` (docs/01 §22).

Adapters extract plain counts from provider objects and call these helpers;
``None`` means the provider did not report the value, which stays
unavailable rather than becoming zero.

``input_tokens`` is recorded as *uncached* input so it can be priced at the
input rate without double counting cached tokens (docs/15 §2.4 formula).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)

MS_PER_SECOND = Decimal(1000)

_STATUS_BY_SOURCE = {
    UsageSource.PROVIDER_REPORTED: UsageReportingStatus.PROVIDER_REPORTED,
    UsageSource.MEASURED: UsageReportingStatus.MEASURED,
    UsageSource.DERIVED: UsageReportingStatus.ESTIMATED,
    UsageSource.ESTIMATED: UsageReportingStatus.ESTIMATED,
}


class UsageNormalizationError(ValueError):
    """Provider usage is internally inconsistent."""


def _item(unit: UsageUnit, quantity: Decimal | int, source: UsageSource) -> UsageItem:
    return UsageItem(
        unit=unit,
        quantity=Decimal(quantity),
        source=source,
        estimated=source in {UsageSource.ESTIMATED, UsageSource.DERIVED},
    )


def _report(items: Iterable[UsageItem], source: UsageSource) -> UsageReport:
    collected = tuple(items)
    if not collected:
        return UsageReport.unavailable()
    return UsageReport(reporting_status=_STATUS_BY_SOURCE[source], items=collected)


def llm_usage(
    *,
    total_input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    source: UsageSource = UsageSource.PROVIDER_REPORTED,
) -> UsageReport:
    """Split provider total input into uncached and cached token meters.

    Cache-write tokens are a separate reported meter (docs/15 §2.4 formula);
    ``None`` means the provider did not report them, so no item is created.
    """
    items: list[UsageItem] = []
    if total_input_tokens is not None:
        cached = cached_input_tokens or 0
        if cached > total_input_tokens:
            raise UsageNormalizationError("cached input tokens exceed total input tokens")
        items.append(_item(UsageUnit.INPUT_TOKENS, total_input_tokens - cached, source))
    if cached_input_tokens is not None:
        items.append(_item(UsageUnit.CACHED_INPUT_TOKENS, cached_input_tokens, source))
    if cache_write_tokens is not None:
        items.append(_item(UsageUnit.CACHE_WRITE_TOKENS, cache_write_tokens, source))
    if output_tokens is not None:
        items.append(_item(UsageUnit.OUTPUT_TOKENS, output_tokens, source))
    if reasoning_tokens is not None:
        items.append(_item(UsageUnit.REASONING_TOKENS, reasoning_tokens, source))
    return _report(items, source)


def stt_usage(
    *,
    transcribed_audio_ms: int | None,
    connected_audio_ms: int | None = None,
    source: UsageSource = UsageSource.PROVIDER_REPORTED,
) -> UsageReport:
    items: list[UsageItem] = []
    if transcribed_audio_ms is not None:
        seconds = Decimal(transcribed_audio_ms) / MS_PER_SECOND
        items.append(_item(UsageUnit.TRANSCRIBED_AUDIO_SECONDS, seconds, source))
    if connected_audio_ms is not None:
        seconds = Decimal(connected_audio_ms) / MS_PER_SECOND
        items.append(_item(UsageUnit.CONNECTED_AUDIO_SECONDS, seconds, source))
    return _report(items, source)


def tts_usage(
    *,
    synthesized_characters: int | None,
    generated_audio_ms: int | None = None,
    source: UsageSource = UsageSource.PROVIDER_REPORTED,
) -> UsageReport:
    items: list[UsageItem] = []
    if synthesized_characters is not None:
        items.append(_item(UsageUnit.SYNTHESIZED_CHARACTERS, synthesized_characters, source))
    if generated_audio_ms is not None:
        seconds = Decimal(generated_audio_ms) / MS_PER_SECOND
        items.append(_item(UsageUnit.GENERATED_AUDIO_SECONDS, seconds, source))
    return _report(items, source)


def merge_reports(reports: Iterable[UsageReport]) -> UsageReport:
    """Sum available reports by unit; unavailable reports contribute nothing.

    The merged status is ``estimated`` when any contributing item is an
    estimate, otherwise ``provider_reported`` if any is, else ``measured``.
    """
    totals: dict[UsageUnit, Decimal] = {}
    estimated_units: set[UsageUnit] = set()
    sources: set[UsageSource] = set()
    for report in reports:
        for item in report.items:
            totals[item.unit] = totals.get(item.unit, Decimal(0)) + item.quantity
            sources.add(item.source)
            if item.estimated:
                estimated_units.add(item.unit)
    if not totals:
        return UsageReport.unavailable()
    source = _dominant_source(sources)
    items = tuple(
        UsageItem(
            unit=unit,
            quantity=quantity,
            source=UsageSource.ESTIMATED if unit in estimated_units else source,
            estimated=unit in estimated_units,
        )
        for unit, quantity in totals.items()
    )
    status = UsageReportingStatus.ESTIMATED if estimated_units else _STATUS_BY_SOURCE[source]
    return UsageReport(reporting_status=status, items=items)


def _dominant_source(sources: set[UsageSource]) -> UsageSource:
    if UsageSource.PROVIDER_REPORTED in sources:
        return UsageSource.PROVIDER_REPORTED
    if UsageSource.MEASURED in sources:
        return UsageSource.MEASURED
    return UsageSource.ESTIMATED
