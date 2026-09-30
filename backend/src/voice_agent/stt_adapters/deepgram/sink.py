"""Bounded normalized-event queue for the Deepgram adapter (docs/05 §9, docs/07 §7).

- ordinary events wait for space (back-pressure, nothing silently lost);
- partials are lossy and dropped with a counter when the queue is full;
- ``stt.warning`` is rate-capped per stream; extra warnings are counted;
- terminal items (final usage/closed and the end marker) always fit.

Counters are mirrored onto the current stream attempt so they land in its
``stt.stream_closed`` evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import SttEvent, SttWarning
from voice_agent.stt_adapters.deepgram.stream import StreamAttempt


class EventSink:
    def __init__(
        self,
        *,
        base: GenerationStamp,
        capacity: int,
        warning_cap: int,
        current: Callable[[], StreamAttempt | None],
    ) -> None:
        self._base = base
        self._events: asyncio.Queue[SttEvent | None] = asyncio.Queue(capacity)
        self._warning_cap = warning_cap
        self._warnings = 0
        self._current = current
        self.counters: dict[str, int] = {}

    @property
    def base(self) -> GenerationStamp:
        return self._base

    def stream_stamp(self) -> GenerationStamp:
        attempt = self._current()
        return self._base if attempt is None else attempt.stamp

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount
        attempt = self._current()
        if attempt is not None:
            attempt.counters[name] += amount

    async def emit(self, event: SttEvent) -> None:
        await self._events.put(event)

    def emit_lossy(self, event: SttEvent) -> None:
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.count("partials_dropped")

    async def warn(self, code: str) -> None:
        if self._warnings >= self._warning_cap:
            self.count("warnings_suppressed")
            return
        self._warnings += 1
        await self.emit(SttWarning(stamp=self.stream_stamp(), code=code))

    def force_put(self, item: SttEvent | None) -> None:
        """Terminal items always fit: drop the oldest undelivered event if needed."""
        while self._events.full():
            self._events.get_nowait()
            self.counters["events_dropped_on_close"] = (
                self.counters.get("events_dropped_on_close", 0) + 1
            )
        self._events.put_nowait(item)

    async def drain(self) -> AsyncIterator[SttEvent]:
        while (event := await self._events.get()) is not None:
            yield event
