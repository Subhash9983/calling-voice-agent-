"""Sarvam Bulbul v3 adapter over the scripted fake seam (offline; docs/09 §11-§14, §18, §24)."""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from decimal import Decimal

import pytest
from tests.support.fake_sarvam import (
    END,
    PAUSE,
    FakeSarvamConnector,
    audio,
    error,
    lost,
    pcm,
    reply,
)

from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.failures import ErrorType
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsFailed,
    TtsSegmentCompleted,
    TtsSegmentRequest,
    TtsVoiceConfig,
)
from voice_agent.contracts.usage import UsageReportingStatus, UsageUnit
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.ports.tts import TTSPort
from voice_agent.tts_adapters.sarvam.adapter import SarvamTimeouts, SarvamTtsAdapter
from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    FinalMessage,
    SarvamErrorKind,
    SarvamTransportError,
)
from voice_agent.tts_adapters.sarvam.options import UnsupportedVoiceConfigError

pytestmark = pytest.mark.asyncio
SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
VOICE = TtsVoiceConfig(provider="sarvam", model="bulbul:v3", voice_id="priya")
FAST = SarvamTimeouts(first_audio_ms=50, stall_ms=50, total_ms=200)


def _request(
    text: str = "नमस्ते, आप कैसे हैं?",
    *,
    segment: str = "00000000-0000-4000-8000-000000000004",
    language: TtsLanguageCode = TtsLanguageCode.HI_IN,
) -> TtsSegmentRequest:
    return TtsSegmentRequest(
        stamp=GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id="00000000-0000-4000-8000-000000000003",
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id="00000000-0000-4000-8000-000000000005",
        segment_id=segment,
        sequence=0,
        text=text,
        language_code=language,
    )


async def _adapter(
    connector: FakeSarvamConnector, *, timeouts: SarvamTimeouts | None = None, prewarm: bool = False
) -> SarvamTtsAdapter:
    adapter = SarvamTtsAdapter(
        connector, clock=SystemClock(), timeouts=timeouts, prewarm=prewarm, keepalive_s=3600
    )
    await adapter.open_session(VOICE)
    return adapter


async def _collect(adapter: SarvamTtsAdapter, request: TtsSegmentRequest) -> list[TtsEvent]:
    return [event async for event in adapter.synthesize(request)]


def _failure(events: list[TtsEvent]) -> TtsFailed:
    last = events[-1]
    assert isinstance(last, TtsFailed)
    return last


async def test_adapter_satisfies_the_port() -> None:
    assert isinstance(SarvamTtsAdapter(FakeSarvamConnector(), clock=SystemClock()), TTSPort)


async def test_unaligned_chunks_become_24khz_20ms_frames_with_measured_usage() -> None:
    connector = FakeSarvamConnector.with_scripts([reply(1.5, 2.75)])
    adapter = await _adapter(connector)
    request = _request()

    events = await _collect(adapter, request)

    chunks = [e for e in events if isinstance(e, TtsAudioChunk)]
    assert len(chunks) == 5  # 4.25 frames -> 4 whole + 1 padded tail
    assert all(c.frame.sample_rate_hz == 24_000 and c.frame.duration_ms == 20 for c in chunks)
    assert [c.frame.captured_at_ms for c in chunks] == [0, 20, 40, 60, 80]
    assert [c.frame.sequence for c in chunks] == [0, 1, 2, 3, 4]
    assert chunks[-1].frame.pcm.endswith(bytes(720))
    completed = events[-1]
    assert isinstance(completed, TtsSegmentCompleted)
    assert completed.provider_request_id == "req-1"
    assert completed.usage.reporting_status is UsageReportingStatus.MEASURED
    assert completed.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == len(request.text)
    assert completed.usage.quantity_of(UsageUnit.GENERATED_AUDIO_SECONDS) == Decimal("0.1")
    stream = connector.streams[0]
    assert stream.texts == [request.text]
    assert stream.flushes == 1
    settings = stream.configured[0]
    assert (settings.language_code, settings.speaker, settings.sample_rate_hz) == (
        "hi-IN",
        "priya",
        24_000,
    )
    assert (settings.output_audio_codec, settings.pace) == ("linear16", 1.0)


async def test_clean_connection_is_reused_and_reconfigured_only_on_language_change() -> None:
    connector = FakeSarvamConnector.with_scripts([reply(1), reply(1), reply(1)])
    adapter = await _adapter(connector)

    await _collect(adapter, _request())
    await _collect(adapter, _request("फिर से", segment="00000000-0000-4000-8000-0000000000a2"))
    english = _request(
        "Hello again.",
        segment="00000000-0000-4000-8000-0000000000a3",
        language=TtsLanguageCode.EN_IN,
    )
    events = await _collect(adapter, english)

    assert connector.opens == 1
    assert [s.language_code for s in connector.streams[0].configured] == ["hi-IN", "en-IN"]
    assert isinstance(events[-1], TtsSegmentCompleted)
    assert [c.frame.captured_at_ms for c in events if isinstance(c, TtsAudioChunk)] == [40]
    assert adapter.counters["connections_reused"] == 2


async def test_prewarm_opens_a_connection_and_tolerates_failure() -> None:
    warm = FakeSarvamConnector()
    await _adapter(warm, prewarm=True)
    cold = FakeSarvamConnector(open_failures=deque([lost()]))
    adapter = await _adapter(cold, prewarm=True)

    assert warm.opens == 1
    assert adapter.counters["prewarm_failed"] == 1


async def test_only_the_approved_voice_profile_is_accepted() -> None:
    adapter = SarvamTtsAdapter(FakeSarvamConnector(), clock=SystemClock())

    for bad in (
        VOICE.model_copy(update={"voice_id": "shubh"}),
        VOICE.model_copy(update={"model": "bulbul:v2"}),
        VOICE.model_copy(update={"speaking_rate": Decimal("2.5")}),
    ):
        with pytest.raises(UnsupportedVoiceConfigError):
            await adapter.open_session(bad)
    with pytest.raises(RuntimeError, match="not open"):
        await _collect(adapter, _request())


async def test_cancel_mid_stream_discards_the_connection() -> None:
    connector = FakeSarvamConnector.with_scripts([[audio(2), PAUSE], reply(1)])
    adapter = await _adapter(connector)
    request = _request()
    stream = adapter.synthesize(request)

    first = await anext(stream)
    second = await anext(stream)
    await adapter.cancel_segment(request.segment_id)
    after = await anext(stream)
    await stream.aclose()

    assert isinstance(first, TtsAudioChunk)
    assert isinstance(second, TtsAudioChunk)
    assert isinstance(after, TtsCancelled)
    assert after.usage.reporting_status is UsageReportingStatus.MEASURED
    assert connector.streams[0].closed
    events = await _collect(adapter, _request(segment="00000000-0000-4000-8000-0000000000b2"))
    assert connector.opens == 2
    assert isinstance(events[-1], TtsSegmentCompleted)


async def test_cancel_turn_cancels_its_active_segment() -> None:
    connector = FakeSarvamConnector.with_scripts([[PAUSE]])
    adapter = await _adapter(connector, timeouts=SarvamTimeouts(first_audio_ms=5000))
    stream = adapter.synthesize(_request())
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.01)

    await adapter.cancel_turn(TURN)
    cancelled = await pending
    await stream.aclose()

    assert isinstance(cancelled, TtsCancelled)
    # Text was sent but never acknowledged: billing is possible, so it is an estimate.
    assert cancelled.usage.reporting_status is UsageReportingStatus.ESTIMATED
    assert cancelled.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == len(_request().text)


async def test_cancel_of_a_finished_segment_is_a_no_op() -> None:
    adapter = await _adapter(FakeSarvamConnector.with_scripts([reply(1)]))
    await _collect(adapter, _request())

    await adapter.cancel_segment(_request().segment_id)
    await adapter.cancel_turn("00000000-0000-4000-8000-0000000000ff")


async def test_first_audio_timeout_is_not_retryable() -> None:
    connector = FakeSarvamConnector.with_scripts([[PAUSE]])
    adapter = await _adapter(connector, timeouts=FAST)

    failure = _failure(await _collect(adapter, _request())).failure

    assert failure.error_type is ErrorType.PROVIDER_TIMEOUT
    assert failure.failure_phase == "first_audio_timeout"
    assert not failure.retryable
    assert connector.streams[0].closed


async def test_stall_after_audio_is_a_total_timeout() -> None:
    connector = FakeSarvamConnector.with_scripts([[audio(1), PAUSE]])
    adapter = await _adapter(connector, timeouts=FAST)

    events = await _collect(adapter, _request())

    failed = _failure(events)
    assert failed.failure.failure_phase == "total_timeout"
    assert failed.usage.reporting_status is UsageReportingStatus.MEASURED
    assert any(isinstance(e, TtsAudioChunk) for e in events)


@pytest.mark.parametrize(
    ("code", "error_type", "retryable"),
    [
        (429, ErrorType.RATE_LIMITED, True),
        (401, ErrorType.AUTHENTICATION_FAILED, False),
        (402, ErrorType.QUOTA_EXHAUSTED, False),
        (422, ErrorType.CONFIGURATION_INVALID, False),
        (503, ErrorType.PROVIDER_UNAVAILABLE, True),
        (None, ErrorType.UNKNOWN_PROVIDER_ERROR, False),
    ],
)
async def test_provider_error_messages_are_normalized(
    code: int | None, error_type: ErrorType, retryable: bool
) -> None:
    adapter = await _adapter(FakeSarvamConnector.with_scripts([[error(code)]]))

    failure = _failure(await _collect(adapter, _request())).failure

    assert (failure.error_type, failure.retryable, failure.status_code) == (
        error_type,
        retryable,
        code,
    )
    assert failure.provider == "sarvam"


async def test_lost_and_ended_streams_are_retryable_connection_losses() -> None:
    adapter = await _adapter(FakeSarvamConnector.with_scripts([[lost()], [END]]))

    raised = _failure(await _collect(adapter, _request())).failure
    ended = _failure(
        await _collect(adapter, _request(segment="00000000-0000-4000-8000-0000000000c2"))
    ).failure

    assert raised.error_type is ErrorType.CONNECTION_LOST
    assert raised.retryable
    assert ended.failure_phase == "stream_ended"


async def test_connect_failure_reports_measured_zero_usage() -> None:
    connector = FakeSarvamConnector(
        open_failures=deque([SarvamTransportError(SarvamErrorKind.AUTHENTICATION, 401)])
    )
    adapter = await _adapter(connector)

    failed = _failure(await _collect(adapter, _request()))

    assert failed.failure.error_type is ErrorType.AUTHENTICATION_FAILED
    assert failed.failure.failure_phase == "connect"
    assert failed.usage.reporting_status is UsageReportingStatus.MEASURED
    assert failed.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == 0


async def test_send_failure_is_normalized() -> None:
    connector = FakeSarvamConnector()
    adapter = await _adapter(connector, prewarm=True)
    connector.streams[0].fail_send = lost()

    failed = _failure(await _collect(adapter, _request()))

    assert failed.failure.failure_phase == "send"
    assert failed.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == 0


async def test_non_pcm_audio_is_corrupt() -> None:
    script = [[audio(1, content_type="audio/mpeg"), FinalMessage()]]
    adapter = await _adapter(FakeSarvamConnector.with_scripts(script))

    failure = _failure(await _collect(adapter, _request())).failure

    assert failure.failure_phase == "decode"
    assert not failure.retryable


def _wav(body: bytes, *, rate: int = 24_000, channels: int = 1) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
    return (
        b"RIFF"
        + (36 + len(body)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + fmt
        + b"data"
        + len(body).to_bytes(4, "little")
        + body
    )


async def test_a_wav_header_is_validated_and_stripped() -> None:
    good = AudioMessage(pcm=_wav(pcm(2)), content_type="audio/wav")
    bad = AudioMessage(pcm=_wav(pcm(2), rate=22_050), content_type="audio/wav")
    adapter = await _adapter(FakeSarvamConnector.with_scripts([[good, FinalMessage()], [bad]]))

    events = await _collect(adapter, _request())
    failed = await _collect(adapter, _request(segment="00000000-0000-4000-8000-0000000000d2"))

    assert len([e for e in events if isinstance(e, TtsAudioChunk)]) == 2
    assert _failure(failed).failure.failure_phase == "decode"


async def test_abandoned_stream_discards_its_connection() -> None:
    connector = FakeSarvamConnector.with_scripts([[audio(3), PAUSE]])
    adapter = await _adapter(connector)
    stream = adapter.synthesize(_request())

    await anext(stream)
    await stream.aclose()

    assert connector.streams[0].closed


async def test_idle_keepalive_pings_and_a_failed_ping_drops_the_connection() -> None:
    connector = FakeSarvamConnector()
    adapter = SarvamTtsAdapter(connector, clock=SystemClock(), keepalive_s=0.01)
    await adapter.open_session(VOICE)
    await asyncio.sleep(0.05)
    stream = connector.streams[0]
    assert stream.pings >= 1

    stream.fail_ping = True
    await asyncio.sleep(0.05)

    assert stream.closed
    await adapter.close()


async def test_close_is_idempotent_and_blocks_synthesis() -> None:
    connector = FakeSarvamConnector()
    adapter = await _adapter(connector, prewarm=True)

    await adapter.close()
    await adapter.close()

    assert connector.closed
    assert connector.streams[0].closed
    with pytest.raises(RuntimeError, match="not open"):
        await _collect(adapter, _request())
