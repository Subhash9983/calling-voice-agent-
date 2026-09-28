"""TTS port contract (docs/09 §3, §11, §25). WP9's Sarvam adapter joins this suite."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsSegmentCompleted,
    TtsSegmentRequest,
    TtsVoiceConfig,
)
from voice_agent.contracts.usage import UsageUnit
from voice_agent.ports.tts import TTSPort
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
OPERATION = "00000000-0000-4000-8000-000000000003"
SEGMENT = "00000000-0000-4000-8000-000000000004"
VOICE = TtsVoiceConfig(provider="mock_tts", model="mock-tts-v1", voice_id="mock")


def _request(text: str = "नमस्ते, आप कैसे हैं?") -> TtsSegmentRequest:
    return TtsSegmentRequest(
        stamp=GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id=OPERATION,
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id=OPERATION,
        segment_id=SEGMENT,
        sequence=0,
        text=text,
        language_code="hi-IN",  # type: ignore[arg-type]
    )


async def _collect(adapter: MockTtsAdapter, request: TtsSegmentRequest) -> list[TtsEvent]:
    return [event async for event in adapter.synthesize(request)]


def test_adapter_satisfies_the_port() -> None:
    assert isinstance(MockTtsAdapter(), TTSPort)


@pytest.mark.asyncio
async def test_synthesis_requires_an_open_session() -> None:
    with pytest.raises(RuntimeError, match="not open"):
        await _collect(MockTtsAdapter(), _request())


@pytest.mark.asyncio
async def test_frames_are_24khz_mono_20ms_and_usage_counts_characters() -> None:
    adapter = MockTtsAdapter(frames_per_segment=3)
    await adapter.open_session(VOICE)
    request = _request()

    events = await _collect(adapter, request)

    chunks = [e for e in events if isinstance(e, TtsAudioChunk)]
    assert len(chunks) == 3
    assert all(c.frame.sample_rate_hz == 24_000 and c.frame.duration_ms == 20 for c in chunks)
    assert all(c.frame.channels == 1 for c in chunks)
    timestamps = [c.frame.captured_at_ms for c in chunks]
    assert timestamps == sorted(timestamps)
    completed = events[-1]
    assert isinstance(completed, TtsSegmentCompleted)
    assert completed.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == len(request.text)
    assert all(e.stamp == request.stamp for e in events)


@pytest.mark.asyncio
async def test_cancel_segment_stops_unplayed_audio() -> None:
    adapter = MockTtsAdapter(frames_per_segment=4, pause_when=lambda _r: True)
    await adapter.open_session(VOICE)
    stream = adapter.synthesize(_request())
    first = await anext(stream)

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await adapter.cancel_segment(SEGMENT)
    after_cancel = await pending

    assert isinstance(first, TtsAudioChunk)
    assert isinstance(after_cancel, TtsCancelled)
    assert after_cancel.usage.quantity_of(UsageUnit.GENERATED_AUDIO_SECONDS) == Decimal("0.02")
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancel_turn_cancels_every_segment_of_the_turn() -> None:
    adapter = MockTtsAdapter(frames_per_segment=2, pause_when=lambda _r: True)
    await adapter.open_session(VOICE)
    stream = adapter.synthesize(_request())
    await anext(stream)

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await adapter.cancel_turn(TURN)

    assert isinstance(await pending, TtsCancelled)
    await stream.aclose()


@pytest.mark.asyncio
async def test_uncooperative_provider_keeps_emitting_after_cancel() -> None:
    adapter = MockTtsAdapter(frames_per_segment=3, pause_when=lambda _r: True, honor_cancel=False)
    await adapter.open_session(VOICE)
    stream = adapter.synthesize(_request())
    await anext(stream)

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await adapter.cancel_segment(SEGMENT)

    assert isinstance(await pending, TtsAudioChunk)
    await stream.aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_blocks_synthesis() -> None:
    adapter = MockTtsAdapter()
    await adapter.open_session(VOICE)

    await adapter.close()
    await adapter.close()

    with pytest.raises(RuntimeError):
        await _collect(adapter, _request())
    with pytest.raises(ValueError, match="frame"):
        MockTtsAdapter(frames_per_segment=0)
