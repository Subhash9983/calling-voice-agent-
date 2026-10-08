"""Browser liveness heartbeat for the speech media-modes (stt/llm/tts/conversation).

The tone/echo media check already sends a lossy ``va.metrics.v1`` message
every interval; the browser treats any agent message as proof of life. The
speech modes reuse the same topic and event type so an idle-but-healthy
session never looks like a lost agent.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport

from voice_agent.agent_worker.liveness_heartbeat import browser_heartbeat
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator

pytestmark = pytest.mark.asyncio
INTERVAL_S = 0.01


async def _run_until(transport: FakeSessionTransport, beats: int) -> None:
    """Run the heartbeat until ``beats`` messages were sent (bounded, not timing-based)."""
    task = asyncio.create_task(
        browser_heartbeat(
            transport,
            session_id=SESSION_ID,
            correlation_id="heartbeat-test",
            clock=SystemClock(),
            ids=UuidIdGenerator(),
            lease_hint=lambda: 9000,
            interval_s=INTERVAL_S,
        )
    )
    try:
        async with asyncio.timeout(5):
            while len(transport.on("va.metrics.v1")) < beats:  # noqa: ASYNC110 - polls a fake
                await asyncio.sleep(INTERVAL_S)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_sends_periodic_lossy_metrics_with_the_lease_hint() -> None:
    transport = FakeSessionTransport()

    await _run_until(transport, beats=2)

    beats = transport.on("va.metrics.v1")
    assert len(beats) >= 2
    assert all(not beat.reliable for beat in beats)
    body = beats[0].body
    assert body["event_type"] == "transport.quality_updated"  # same as the tone/echo path
    assert body["session_id"] == SESSION_ID
    assert body["payload"] == {"lease_valid_for_ms": 9000}
    assert transport.sent == beats  # nothing but the heartbeat


async def test_a_failed_send_never_stops_the_heartbeat() -> None:
    class Flaky(FakeSessionTransport):
        failures = 0

        async def send_event(
            self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
        ) -> None:
            if self.failures < 2:
                self.failures += 1
                raise RuntimeError("data channel hiccup")
            await super().send_event(topic, envelope, reliable=reliable)

    transport = Flaky()

    await _run_until(transport, beats=1)  # times out (fails) if a send error stopped it

    assert transport.failures == 2
    assert transport.on("va.metrics.v1")
