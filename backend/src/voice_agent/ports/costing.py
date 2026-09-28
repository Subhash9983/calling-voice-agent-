"""Cost calculator and rate-lookup ports (docs/03 §12, §20)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from voice_agent.contracts.cost import (
    NORMALIZED_COST_CURRENCY,
    CostCalculation,
    Currency,
    RateCard,
)
from voice_agent.contracts.enums import OperationComponent
from voice_agent.contracts.usage import UsageReport


@dataclass(frozen=True, slots=True)
class MeteredUsage:
    """Usage attributed to one provider attempt, ready for pricing."""

    component: OperationComponent
    provider: str
    model: str | None
    usage: UsageReport


@runtime_checkable
class RateLookup(Protocol):
    def rate_card(self, rate_card_id: str) -> RateCard | None:
        """Return the exact dated rate card, or ``None`` when it is unknown."""
        ...


@runtime_checkable
class CostCalculatorPort(Protocol):
    def calculate(
        self,
        usages: Sequence[MeteredUsage],
        *,
        reporting_currency: Currency = NORMALIZED_COST_CURRENCY,
    ) -> CostCalculation:
        """Price every attempt; missing usage or rates stay missing, never zero."""
        ...
