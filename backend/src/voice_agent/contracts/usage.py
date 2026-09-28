"""Normalized provider usage (docs/01 §22, docs/02 §8 embedded ``usage``).

Adapters report provider-native billable units. A missing value is omitted
and the report is marked ``unavailable``; it is never converted to zero.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from voice_agent.contracts.base import NonNegativeDecimal, StrictModel

MAX_USAGE_ITEMS = 20


class UsageUnit(StrEnum):
    CONNECTED_AUDIO_SECONDS = "connected_audio_seconds"
    TRANSCRIBED_AUDIO_SECONDS = "transcribed_audio_seconds"
    INPUT_TOKENS = "input_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    # docs/08 §12, §16 and docs/15 §2.4 (priced separately); not yet in docs/02 §8.
    CACHE_WRITE_TOKENS = "cache_write_tokens"
    OUTPUT_TOKENS = "output_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    SYNTHESIZED_CHARACTERS = "synthesized_characters"
    GENERATED_AUDIO_SECONDS = "generated_audio_seconds"
    TRANSPORT_SESSION_SECONDS = "transport_session_seconds"
    RECORDED_AUDIO_SECONDS = "recorded_audio_seconds"
    REQUESTS = "requests"


class UsageReportingStatus(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    MEASURED = "measured"
    DERIVED = "derived"
    ESTIMATED = "estimated"


class UsageItem(StrictModel):
    unit: UsageUnit
    quantity: NonNegativeDecimal
    source: UsageSource
    estimated: bool = False

    @model_validator(mode="after")
    def _estimate_flag_matches_source(self) -> UsageItem:
        if self.source is UsageSource.ESTIMATED and not self.estimated:
            raise ValueError("an estimated usage source must set estimated=True")
        return self


class UsageReport(StrictModel):
    reporting_status: UsageReportingStatus
    items: Annotated[tuple[UsageItem, ...], Field(max_length=MAX_USAGE_ITEMS)] = ()

    @model_validator(mode="after")
    def _availability_is_explicit(self) -> UsageReport:
        if self.reporting_status is UsageReportingStatus.UNAVAILABLE and self.items:
            raise ValueError("an unavailable usage report cannot carry quantities")
        if self.reporting_status is not UsageReportingStatus.UNAVAILABLE and not self.items:
            raise ValueError("an available usage report needs at least one item")
        units = [item.unit for item in self.items]
        if len(units) != len(set(units)):
            raise ValueError("usage units must be unique within one report")
        return self

    @classmethod
    def unavailable(cls) -> UsageReport:
        return cls(reporting_status=UsageReportingStatus.UNAVAILABLE)

    @property
    def is_available(self) -> bool:
        return self.reporting_status is not UsageReportingStatus.UNAVAILABLE

    def quantity_of(self, unit: UsageUnit) -> Decimal | None:
        """Return the reported quantity, or ``None`` when it is unavailable."""
        for item in self.items:
            if item.unit is unit:
                return item.quantity
        return None
