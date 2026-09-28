"""Worker transport and speech-activity port contracts (docs/06 §18, docs/03 §8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from voice_agent.contracts.audio import AudioFrame, square_wave_pcm
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.transport import (
    ClientEvent,
    ClientReady,
    PlaybackAck,
    PlaybackAckKind,
    PlaybackFrame,
    RealtimeTopic,
)
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.ports.transport import WorkerTransportPort
from voice_agent.speech_activity.mock import MockSpeechActivityDetector
from voice_agent.transport_adapters.mock.adapter import (
    MockTransport,
    Signal,
    Silence,
    Speak,
    WaitUntil,
)

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
SEGMENT = "00000000-0000-4000-8000-000000000004"
IDENTITY = PlaybackAckIdentity(worker_generation=1, cancellation_generation=0, segment_id=SEGMENT)


def _playback_frame() -> PlaybackFrame:
    frame = AudioFrame(
        session_id=SESSION,
        track_label="agent",
        sequence=0,
        sample_rate_hz=24_000,
        duration_ms=20,
        captured_at_ms=0,
        pcm=square_wave_pcm(0.3, 480),
    )
    return PlaybackFrame(identity=IDENTITY, turn_id=TURN, frame=frame)


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id=SEGMENT,
        event_type=EventType.TURN_COMPLETED,
        occurred_at=datetime(2026, 9, 28, tzinfo=UTC),
        session_id=SESSION,
        correlation_id="c",
        component="orchestrator",
        producer_service="agent_worker",
    )


async def _client_events(transport: MockTransport) -> list[ClientEvent]:
    return [event async for event in transport.client_events()]


def test_mocks_satisfy_their_ports() -> None:
    assert isinstance(MockTransport(SESSION, []), WorkerTransportPort)
    assert isinstance(MockSpeechActivityDetector(TurnHandlingPolicy()), SpeechActivityPort)


@pytest.mark.asyncio
async def test_microphone_script_yields_contiguous_16khz_frames() -> None:
    transport = MockTransport(SESSION, [Speak(60), Silence(40)])

    frames = [frame async for frame in transport.audio_frames()]

    assert [f.captured_at_ms for f in frames] == [0, 20, 40, 60, 80]
    assert {f.sample_rate_hz for f in frames} == {16_000}
    assert [f.sequence for f in frames] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_wait_until_gates_input_on_observed_playback() -> None:
    log: list[str] = []
    transport = MockTransport(
        SESSION,
        [
            Signal("go", lambda: log.append("signal")),
            WaitUntil("playing", lambda t: t.started_segment_count > 0),
            Speak(20),
        ],
        record=log.append,
    )
    reader = asyncio.ensure_future(_frames(transport))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not reader.done()

    await transport.publish_audio(_playback_frame())
    frames = await reader

    assert len(frames) == 1
    assert log[:2] == ["transport.signal:go", "signal"]


async def _frames(transport: MockTransport) -> list[AudioFrame]:
    return [frame async for frame in transport.audio_frames()]


@pytest.mark.asyncio
async def test_playback_is_acknowledged_like_a_browser() -> None:
    transport = MockTransport(SESSION, [])

    await transport.publish_audio(_playback_frame())
    await transport.publish_audio(_playback_frame())
    await transport.finish_segment(IDENTITY)
    transport.inject_client_event(ClientReady())
    await transport.close()
    events = await _client_events(transport)

    acks = [e for e in events if isinstance(e, PlaybackAck)]
    assert [a.ack for a in acks] == [PlaybackAckKind.STARTED, PlaybackAckKind.COMPLETED]
    assert all(a.identity == IDENTITY for a in acks)
    assert isinstance(events[-1], ClientReady)


@pytest.mark.asyncio
async def test_clear_send_and_close_semantics() -> None:
    transport = MockTransport(SESSION, [], auto_ack=False)
    await transport.publish_audio(_playback_frame())

    await transport.clear_playback()
    await transport.send_event(RealtimeTopic.STATE, _envelope(), reliable=True)
    await transport.close()
    await transport.close()

    assert transport.clear_marks == [1]
    assert transport.sent_count("turn.completed") == 1
    assert await _client_events(transport) == []
    with pytest.raises(RuntimeError, match="closed"):
        await transport.publish_audio(_playback_frame())
