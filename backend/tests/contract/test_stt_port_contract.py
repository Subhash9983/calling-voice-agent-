"""STT port contract (docs/07 §3, §21). Real adapters must pass the same suite in WP7."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from voice_agent.contracts.audio import AudioFrame, square_wave_pcm
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    SttEvent,
    SttFinalSegment,
    SttStreamConfig,
    SttTurnFinalized,
    SttUsage,
)
from voice_agent.contracts.usage import UsageUnit
from voice_agent.ports.stt import STTPort
from voice_agent.stt_adapters.mock.adapter import MockSttAdapter

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
STREAM_OP = "00000000-0000-4000-8000-000000000003"

AdapterFactory = Callable[..., Any]
FACTORIES: list[AdapterFactory] = [MockSttAdapter]


def _stamp(turn_id: str | None = TURN) -> GenerationStamp:
    return GenerationStamp(
        session_id=SESSION,
        turn_id=turn_id,
        operation_id=STREAM_OP,
        worker_generation=1,
        cancellation_generation=0,
    )


def _frame(sequence: int) -> AudioFrame:
    return AudioFrame(
        session_id=SESSION,
        sequence=sequence,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=sequence * 20,
        pcm=square_wave_pcm(0.5, 320),
    )


async def _drain(adapter: STTPort) -> list[SttEvent]:
    events: list[SttEvent] = []
    async for event in adapter.events():
        events.append(event)
    return events


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__)
def factory(request: pytest.FixtureRequest) -> AdapterFactory:
    return request.param  # type: ignore[no-any-return]


def test_adapter_satisfies_the_port(factory: AdapterFactory) -> None:
    assert isinstance(factory(["x"]), STTPort)


@pytest.mark.asyncio
async def test_audio_is_rejected_before_start(factory: AdapterFactory) -> None:
    adapter = factory(["hello"])

    with pytest.raises(RuntimeError):
        await adapter.write_audio(_frame(0), _stamp())


@pytest.mark.asyncio
async def test_finalize_emits_segment_turn_final_and_usage_with_the_request_stamp(
    factory: AdapterFactory,
) -> None:
    adapter = factory(["नमस्ते"])
    await adapter.start(SttStreamConfig())
    for sequence in range(10):
        await adapter.write_audio(_frame(sequence), _stamp(None))

    await adapter.finalize_turn(_stamp())
    await adapter.close()
    events = await _drain(adapter)

    assert [type(e) for e in events] == [SttFinalSegment, SttTurnFinalized, SttUsage]
    assert all(e.stamp == _stamp() for e in events)
    final = events[1]
    assert isinstance(final, SttTurnFinalized)
    assert final.text == "नमस्ते"
    usage = events[2]
    assert isinstance(usage, SttUsage)
    assert usage.usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal("0.2")


@pytest.mark.asyncio
async def test_empty_final_has_no_invented_language(factory: AdapterFactory) -> None:
    adapter = factory([""])
    await adapter.start(SttStreamConfig())

    await adapter.finalize_turn(_stamp())
    await adapter.close()
    final = next(e for e in await _drain(adapter) if isinstance(e, SttTurnFinalized))

    assert not final.is_usable
    assert final.language is None


@pytest.mark.asyncio
async def test_cancelled_turn_produces_no_results(factory: AdapterFactory) -> None:
    adapter = factory(["ignored"])
    await adapter.start(SttStreamConfig())

    await adapter.cancel_turn(TURN)
    await adapter.finalize_turn(_stamp())
    await adapter.close()

    assert await _drain(adapter) == []


@pytest.mark.asyncio
async def test_finalize_requires_a_turn_and_close_is_idempotent(factory: AdapterFactory) -> None:
    adapter = factory(["x"])
    await adapter.start(SttStreamConfig())

    with pytest.raises(ValueError, match="turn"):
        await adapter.finalize_turn(_stamp(None))
    await adapter.close()
    await adapter.close()
    assert await asyncio.wait_for(_drain(adapter), timeout=1) == []
    with pytest.raises(RuntimeError):
        await adapter.write_audio(_frame(0), _stamp())


@pytest.mark.asyncio
async def test_misbehaving_provider_can_answer_after_cancel() -> None:
    """Fault-injection mode: the orchestrator, not the adapter, must reject this."""
    adapter = MockSttAdapter(["late"], emit_after_cancel=True)
    await adapter.start(SttStreamConfig())

    await adapter.cancel_turn(TURN)
    await adapter.finalize_turn(_stamp())
    await adapter.close()

    assert any(isinstance(e, SttTurnFinalized) for e in await _drain(adapter))


@pytest.mark.asyncio
async def test_frame_and_stamp_sessions_must_match() -> None:
    adapter = MockSttAdapter(["x"])
    await adapter.start(SttStreamConfig())
    other = _stamp().model_copy(update={"session_id": "00000000-0000-4000-8000-0000000000ff"})

    with pytest.raises(ValueError, match="different sessions"):
        await adapter.write_audio(_frame(0), other)
    assert adapter.config == SttStreamConfig()
