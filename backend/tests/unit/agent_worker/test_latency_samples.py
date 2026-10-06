"""The per-turn browser latency recorder stays bounded (docs/06 §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from voice_agent.agent_worker.latency_samples import MAX_TRACKED_TURNS, LatencySampleRecorder
from voice_agent.agent_worker.ordered_writer import OrderedWriter
from voice_agent.contracts.transport import ClientLatencySample
from voice_agent.events_and_latency.clock import SystemClock

pytestmark = pytest.mark.asyncio


class _Evidence:
    def __init__(self) -> None:
        self.events: list[tuple[Any, str | None, dict[str, Any], datetime | None]] = []

    async def event(
        self,
        event_type: Any,
        *,
        turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        self.events.append((event_type, turn_id, payload or {}, occurred_at))


def _turn(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012x}"


async def test_the_oldest_tracked_turn_is_evicted_beyond_the_bound() -> None:
    evidence, writer, counts = _Evidence(), OrderedWriter(), []
    recorder = LatencySampleRecorder(evidence, writer, SystemClock(), counts.append)  # type: ignore[arg-type]
    for index in range(MAX_TRACKED_TURNS + 1):
        recorder.track(_turn(index))

    evicted = recorder.record(ClientLatencySample(turn_id=_turn(0), browser_playout_ms=10))
    newest = recorder.record(
        ClientLatencySample(turn_id=_turn(MAX_TRACKED_TURNS), browser_playout_ms=10)
    )
    await writer.close()

    assert (evicted, newest) == (False, True)
    assert counts == ["latency_samples_ignored", "latency_samples_recorded"]
    assert [turn_id for _, turn_id, _, _ in evidence.events] == [_turn(MAX_TRACKED_TURNS)]
