"""Live-run helpers for ``-m openai`` tests: SSE shape capture and a hard spend guard (WP8).

- :class:`RecordingConnector` wraps the production SDK connector and records
  only *structural* evidence per stream: event type names, the terminal
  ``status``/``incomplete_details``, and the usage key tree with its numbers.
  Text deltas are counted, never stored here.
- :data:`SPEND` sums each attempt's cost in Decimal at the docs/15 rate card and
  refuses to start a call once spent + a worst-case per-call estimate would
  exceed the session guard (well under the USD 0.25 WP8 hard cap).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from voice_agent.contracts.enums import OperationComponent
from voice_agent.contracts.usage import UsageReport
from voice_agent.conversation_adapters.openai.connection import ResponsesStream
from voice_agent.conversation_adapters.openai.sdk_binding import SdkResponsesConnector
from voice_agent.costing.calculator import CostCalculator
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.ports.costing import MeteredUsage
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import BootstrapSettings

WP8_HARD_CAP_USD: Final = Decimal("0.25")
SESSION_GUARD_USD: Final = Decimal("0.05")
# 16k input tokens at 0.10/M + 250 output tokens at 0.50/M, rounded up.
WORST_CASE_CALL_USD: Final = Decimal("0.002")
TERMINAL_TYPES: Final = frozenset({"response.completed", "response.incomplete", "response.failed"})


LIVE_APPROVAL_VARIABLE: Final = "VOICE_AGENT_OPENAI_LIVE_APPROVED"


def live_settings() -> BootstrapSettings:
    """WP3 settings without the test-only approval switch (the strict loader rejects it)."""
    environ = {k: v for k, v in os.environ.items() if k != LIVE_APPROVAL_VARIABLE}
    return load_bootstrap_configuration(environ).settings


def _shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, bool | int | float) or value is None:
        return value
    return type(value).__name__


@dataclass
class StreamShape:
    event_types: list[str] = field(default_factory=list)
    text_deltas: int = 0
    terminal: dict[str, Any] = field(default_factory=dict)

    def record(self, event: Mapping[str, Any]) -> None:
        kind = str(event.get("type"))
        if not self.event_types or self.event_types[-1] != kind:
            self.event_types.append(kind)
        if kind == "response.output_text.delta":
            self.text_deltas += 1
        if kind in TERMINAL_TYPES:
            response = event.get("response")
            body = response if isinstance(response, Mapping) else {}
            self.terminal = {
                "type": kind,
                "status": body.get("status"),
                "incomplete_details": body.get("incomplete_details"),
                "service_tier": body.get("service_tier"),
                "store": body.get("store"),
                "usage": _shape(body.get("usage")),
                "response_keys": sorted(body),
            }


@dataclass
class RecordingConnector:
    inner: SdkResponsesConnector
    shapes: list[StreamShape] = field(default_factory=list)

    @asynccontextmanager
    async def open(self, params: Mapping[str, Any]) -> AsyncIterator[ResponsesStream]:
        async with self.inner.open(params) as stream:
            shape = StreamShape()
            self.shapes.append(shape)
            yield _RecordingStream(stream, shape)

    async def aclose(self) -> None:
        await self.inner.aclose()


class _RecordingStream:
    def __init__(self, stream: ResponsesStream, shape: StreamShape) -> None:
        self._stream = stream
        self._shape = shape

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        async for event in self._stream.events():
            self._shape.record(event)
            yield event


@dataclass
class SpendTracker:
    spent_usd: Decimal = Decimal(0)
    calls: int = 0
    calculator: CostCalculator = field(default_factory=lambda: CostCalculator(phase0_rate_card()))

    def before_call(self) -> None:
        """Refuse a call that could cross the session guard."""
        if self.spent_usd + WORST_CASE_CALL_USD > SESSION_GUARD_USD:
            raise RuntimeError("WP8 live spend guard reached; no further OpenAI calls")
        self.calls += 1

    def add(self, usage: UsageReport) -> Decimal:
        metered = MeteredUsage(
            component=OperationComponent.CONVERSATION_ENGINE,
            provider="openai",
            model="gpt-6-luna",
            usage=usage,
        )
        cost = self.calculator.calculate([metered]).total or Decimal(0)
        self.spent_usd += cost
        assert self.spent_usd < WP8_HARD_CAP_USD
        return cost


SPEND: Final = SpendTracker()
