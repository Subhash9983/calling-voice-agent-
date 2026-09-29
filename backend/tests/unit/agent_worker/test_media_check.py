"""Media check over the real session transport and the fake gateway (no AI providers)."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.support.fake_livekit import Decimate, FakeGateway

from voice_agent.agent_worker.media_check import MediaCheck, MediaMode, MediaTiming, sine_pcm
from voice_agent.contracts.audio import TTS_SAMPLE_RATE_HZ, peak_amplitude
from voice_agent.contracts.realtime_wire import CLIENT_TOPIC
from voice_agent.events_and_latency.clock import SequentialIdGenerator, SystemClock
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

SESSION_ID = "00000000-0000-4000-8000-000000000001"
BROWSER = "va-user-1"


async def _setup(mode: MediaMode = MediaMode.TONE) -> tuple[FakeGateway, MediaCheck]:
    gateway = FakeGateway(present={BROWSER})
    transport = LiveKitSessionTransport(
        session_id=SESSION_ID,
        browser_identity=BROWSER,
        agent_identity="va-agent-1",
        worker_generation=1,
        gateway=gateway,
        clock=SystemClock(),
        resampler_factory=Decimate,
    )
    await transport.connect()
    check = MediaCheck(
        transport,
        session_id=SESSION_ID,
        correlation_id="corr",
        worker_generation=1,
        clock=SystemClock(),
        ids=SequentialIdGenerator(start=100),
        mode=mode,
        lease_hint=lambda: 9000,
        timing=MediaTiming(burst_ms=100, period_ms=30, metrics_interval_s=0.03),
    )
    return gateway, check


def test_sine_is_bounded_and_continuous() -> None:
    first = sine_pcm(start_sample=0, samples=480, sample_rate_hz=TTS_SAMPLE_RATE_HZ)

    assert len(first) == 960
    assert 0.15 < peak_amplitude(first) <= 0.2


async def _run_briefly(check: MediaCheck, seconds: float) -> None:
    task = asyncio.create_task(check.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_tone_bursts_publish_audio_and_announce_segments() -> None:
    gateway, check = await _setup()

    await _run_briefly(check, 0.2)

    assert check.bursts >= 1
    assert len(gateway.sink.captured) >= 5  # 100 ms bursts of 20 ms frames
    topics = [sent.topic for sent in gateway.sent]
    assert "va.state.v1" in topics
    assert "va.metrics.v1" in topics
    playback = [s.body["payload"] for s in gateway.sent if s.topic == "va.playback.v1"]
    assert playback[0]["state"] == "started"
    assert set(playback[0]) == {
        "state",
        "worker_generation",
        "cancellation_generation",
        "segment_id",
    }
    states = [s.body["payload"]["state"] for s in gateway.sent if s.topic == "va.state.v1"]
    assert states[:2] == ["listening", "speaking"]


@pytest.mark.asyncio
async def test_microphone_intake_and_acks_are_reported_in_metrics() -> None:
    gateway, check = await _setup()
    task = asyncio.create_task(check.run())
    await asyncio.sleep(0.01)  # consumers subscribe first; earlier frames are not buffered
    gateway.open_microphone(BROWSER)
    gateway.speak(frames=5, level=8000)
    ack = {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-00000000000a",
        "session_id": SESSION_ID,
        "event_type": "playback.completed",
        "occurred_at": "2026-09-29T12:00:00Z",
        "payload": {
            "worker_generation": 1,
            "cancellation_generation": 0,
            "segment_id": "00000000-0000-4000-8000-00000000000b",
        },
    }
    gateway.data(BROWSER, CLIENT_TOPIC, json.dumps(ack).encode(), True)
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert check.mic_frames == 5
    assert check.mic_peak > 0.2
    metrics = [s for s in gateway.sent if s.topic == "va.metrics.v1"]
    assert metrics
    assert not metrics[-1].reliable
    payload = metrics[-1].body["payload"]
    assert payload["mic_frames"] == 5
    assert payload["playback_acks"] == {"completed": 1}
    assert payload["lease_valid_for_ms"] == 9000


@pytest.mark.asyncio
async def test_echo_replays_recent_microphone_frames() -> None:
    gateway, check = await _setup(MediaMode.ECHO)
    listener = asyncio.create_task(check._listen())
    await asyncio.sleep(0.01)
    gateway.open_microphone(BROWSER)
    gateway.speak(frames=3, level=8000)
    await asyncio.sleep(0.02)

    await check.burst()
    listener.cancel()

    assert len(gateway.sink.captured) == 3
    assert gateway.sink.captured[0] != bytes(len(gateway.sink.captured[0]))


@pytest.mark.asyncio
async def test_nothing_plays_while_the_browser_is_absent() -> None:
    gateway, check = await _setup()
    gateway.leave(BROWSER)

    await _run_briefly(check, 0.12)

    assert gateway.sink.captured == []
    assert check.bursts == 0


@pytest.mark.asyncio
async def test_completed_is_sent_only_after_the_queue_drains() -> None:
    gateway, check = await _setup()

    await check.burst()

    kinds = [(s.topic, s.body["payload"].get("state")) for s in gateway.sent]
    completed_at = kinds.index(("va.playback.v1", "completed"))
    [drained_after] = gateway.sink.drained_after
    assert drained_after <= completed_at  # the drain returned before completed was sent
    assert kinds[drained_after - 1] == ("va.playback.v1", "started")


@pytest.mark.asyncio
async def test_mic_peak_is_reported_per_interval() -> None:
    gateway, check = await _setup()
    task = asyncio.create_task(check.run())
    await asyncio.sleep(0.01)
    gateway.open_microphone(BROWSER)
    gateway.speak(frames=3, level=16000)
    await asyncio.sleep(0.06)
    gateway.speak(frames=3, level=100)
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    peaks = [s.body["payload"]["mic_peak"] for s in gateway.sent if s.topic == "va.metrics.v1"]
    assert max(peaks) > 0.4
    assert peaks[-1] < 0.01  # the loud interval does not stick
