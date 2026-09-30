"""Opt-in live Deepgram checks (``-m deepgram``; metered, WP7 budget USD 1.00 in docs/15).

Only synthetic audio generated in memory is sent: silence and a pure 440 Hz
tone (no speech, no person, nothing stored). One short stream is about 2.5 s
of billable audio (~USD 0.0004 at the USD 0.0092/min ceiling). The rejected-
credential check sends no audio and is not billable. The key loads only
through the WP3 loader/resolver and is never printed.
"""

from __future__ import annotations

import asyncio
import math
from decimal import Decimal

import pytest
from pydantic import SecretStr

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.failures import ErrorType, NormalizedFailureError
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    SttEvent,
    SttStreamClosed,
    SttStreamConfig,
    SttStreamOutcome,
    SttStreamStarted,
    SttTurnFinalized,
    SttUsage,
)
from voice_agent.contracts.usage import UsageSource, UsageUnit
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.provider_registry.stt_check_config import DEEPGRAM_CREDENTIAL_REF
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.credentials import CredentialError, CredentialResolver
from voice_agent.stt_adapters.deepgram.adapter import DeepgramSttAdapter
from voice_agent.stt_adapters.deepgram.sdk_binding import SdkDeepgramConnector

pytestmark = [pytest.mark.deepgram, pytest.mark.asyncio]
SESSION = "00000000-0000-4000-8000-0000000007a1"
TURN = "00000000-0000-4000-8000-0000000007a2"
FRAME_SAMPLES = 320
SILENCE_FRAMES = 50  # 1.0 s
TONE_FRAMES = 50  # 1.0 s
TAIL_FRAMES = 25  # 0.5 s


def _api_key() -> SecretStr:
    settings = load_bootstrap_configuration().settings
    try:
        return CredentialResolver(settings).resolve(DEEPGRAM_CREDENTIAL_REF)
    except CredentialError:
        pytest.skip("DEEPGRAM_API_KEY is not configured")


def _stamp(turn_id: str | None = None) -> GenerationStamp:
    return GenerationStamp(
        session_id=SESSION, turn_id=turn_id, worker_generation=1, cancellation_generation=0
    )


def _pcm(sequence: int, *, tone: bool) -> bytes:
    if not tone:
        return b"\x00\x00" * FRAME_SAMPLES
    step = 2 * math.pi * 440 / 16_000
    return b"".join(
        round(0.2 * 32767 * math.sin(step * (sequence * FRAME_SAMPLES + i))).to_bytes(
            2, "little", signed=True
        )
        for i in range(FRAME_SAMPLES)
    )


def _frames() -> list[AudioFrame]:
    total = SILENCE_FRAMES + TONE_FRAMES + TAIL_FRAMES
    return [
        AudioFrame(
            session_id=SESSION,
            sequence=index,
            sample_rate_hz=16_000,
            duration_ms=20,
            captured_at_ms=index * 20,
            pcm=_pcm(index, tone=SILENCE_FRAMES <= index < SILENCE_FRAMES + TONE_FRAMES),
        )
        for index in range(total)
    ]


def _adapter(key: SecretStr) -> DeepgramSttAdapter:
    return DeepgramSttAdapter(
        SdkDeepgramConnector(key),
        session_id=SESSION,
        worker_generation=1,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )


async def test_live_stream_finalizes_a_synthetic_clip_and_reports_billable_usage() -> None:
    stt = _adapter(_api_key())
    await stt.start(SttStreamConfig())
    frames = _frames()
    for frame in frames:
        await stt.write_audio(frame, _stamp())
        await asyncio.sleep(0.005)  # faster than real time, but not a single burst

    await stt.finalize_turn(_stamp(TURN))
    await asyncio.sleep(3.5)  # covers the 3 s finalize timeout either way
    await stt.close()
    events: list[SttEvent] = [event async for event in stt.events()]

    started = [e for e in events if isinstance(e, SttStreamStarted)]
    final = next(e for e in events if isinstance(e, SttTurnFinalized))
    usage = next(e for e in events if isinstance(e, SttUsage)).usage
    closed = [e for e in events if isinstance(e, SttStreamClosed)][-1]
    assert len(started) == 1
    assert started[0].connect_ms < 5000
    assert not final.finalization_timed_out  # Deepgram answered Finalize (from_finalize)
    assert closed.outcome is SttStreamOutcome.SUCCEEDED
    assert closed.sent_audio_ms == len(frames) * 20
    assert closed.provider_request_id is not None
    seconds = usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS)
    assert seconds is not None
    assert Decimal(2) <= seconds <= Decimal(3)
    assert usage.items[0].source is UsageSource.PROVIDER_REPORTED


async def test_live_rejected_credential_is_normalized_and_not_retried() -> None:
    stt = _adapter(SecretStr("wp7InvalidKeyForNegativeTest0000000000"))

    with pytest.raises(NormalizedFailureError) as raised:
        await stt.start(SttStreamConfig())

    assert raised.value.failure.error_type is ErrorType.AUTHENTICATION_FAILED
    assert raised.value.failure.status_code == 401
    assert "wp7InvalidKey" not in repr(raised.value.failure)
