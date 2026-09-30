"""Deepgram adapter behaviour over the scripted fake connector (offline, no audio stored)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support.fake_deepgram import FakeDeepgramConnector, results

from voice_agent.contracts.audio import AudioFrame, square_wave_pcm
from voice_agent.contracts.failures import ErrorType, NormalizedFailureError
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.stt import (
    AudioWindow,
    SttEvent,
    SttFailed,
    SttFinalSegment,
    SttPartial,
    SttStreamClosed,
    SttStreamConfig,
    SttStreamOutcome,
    SttStreamStarted,
    SttTurnFinalized,
    SttUsage,
    SttWarning,
)
from voice_agent.contracts.usage import UsageSource, UsageUnit
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.stt_adapters.deepgram.adapter import DeepgramSttAdapter, DeepgramTiming
from voice_agent.stt_adapters.deepgram.connection import DeepgramErrorKind, DeepgramTransportError

pytestmark = pytest.mark.asyncio
SESSION = "00000000-0000-4000-8000-000000000001"
TURN_1 = "00000000-0000-4000-8000-000000000011"
TURN_2 = "00000000-0000-4000-8000-000000000012"
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "deepgram" / "stream_messages.json"
FAST = DeepgramTiming(
    connect_timeout_s=0.05,
    close_timeout_s=0.2,
    finalize_timeout_ms=80,
    maintenance_interval_s=0.01,
    keepalive_idle_ms=40,
    response_timeout_ms=10_000,
    inbound_buffer_ms=200,
)


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return dict(loaded[name])


def stamp(turn_id: str | None = None) -> GenerationStamp:
    return GenerationStamp(
        session_id=SESSION, turn_id=turn_id, worker_generation=1, cancellation_generation=0
    )


def frame(sequence: int, *, at_ms: int | None = None) -> AudioFrame:
    return AudioFrame(
        session_id=SESSION,
        sequence=sequence,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=sequence * 20 if at_ms is None else at_ms,
        pcm=square_wave_pcm(0.3, 320),
    )


def adapter(
    connector: FakeDeepgramConnector, *, timing: DeepgramTiming = FAST, **kwargs: Any
) -> DeepgramSttAdapter:
    kwargs.setdefault("retry_delay_ms", lambda _attempt: 1)
    return DeepgramSttAdapter(
        connector,
        session_id=SESSION,
        worker_generation=1,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        timing=timing,
        **kwargs,
    )


async def drain(stt: DeepgramSttAdapter) -> list[SttEvent]:
    return [event async for event in stt.events()]


async def settle() -> None:
    await asyncio.sleep(0.02)


async def write(stt: DeepgramSttAdapter, first: int, count: int) -> None:
    for sequence in range(first, first + count):
        await stt.write_audio(frame(sequence), stamp())


def of[T](events: list[SttEvent], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


# ---------------------------------------------------------------- lifecycle --
async def test_start_connects_nova_3_multilingual_and_announces_the_attempt() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)

    await stt.start(SttStreamConfig())
    await stt.close()
    events = await drain(stt)

    assert connector.params[0]["model"] == "nova-3"
    assert connector.params[0]["language"] == "multi"
    assert "keyterm" not in connector.params[0]
    started = of(events, SttStreamStarted)
    assert len(started) == 1
    assert started[0].attempt.attempt_number == 1
    assert started[0].keyterm_count == 0
    assert started[0].stamp.operation_id is not None


async def test_audio_is_validated_and_streamed_unchanged() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    with pytest.raises(RuntimeError):
        await stt.write_audio(frame(0), stamp())
    await stt.start(SttStreamConfig())

    await write(stt, 0, 5)
    other = stamp().model_copy(update={"session_id": "00000000-0000-4000-8000-000000000099"})
    with pytest.raises(ValueError, match="sessions"):
        await stt.write_audio(frame(5), other)

    assert connector.current.media_messages == 5
    assert connector.current.audio_bytes == 5 * 640
    await stt.close()
    with pytest.raises(RuntimeError):
        await stt.write_audio(frame(6), stamp())


async def test_close_is_idempotent_and_reports_provider_usage_from_metadata() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 50)

    await stt.close()
    await stt.close()
    events = await asyncio.wait_for(drain(stt), timeout=1)

    usage = of(events, SttUsage)[0].usage
    assert usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal("1")
    assert usage.items[0].source is UsageSource.PROVIDER_REPORTED
    closed = of(events, SttStreamClosed)[-1]
    assert closed.outcome is SttStreamOutcome.SUCCEEDED
    assert closed.sent_audio_ms == 1000
    assert closed.provider_request_id is not None
    assert connector.current.close_stream_sent
    assert connector.current.socket_closed


async def test_close_without_metadata_falls_back_to_measured_sent_audio() -> None:
    connector = FakeDeepgramConnector(answer_close=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 10)

    await stt.close()
    events = await drain(stt)

    usage = of(events, SttUsage)[0].usage
    assert usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal("0.2")
    assert usage.items[0].source is UsageSource.MEASURED
    assert of(events, SttStreamClosed)[-1].counters["close_without_metadata"] == 1


# ------------------------------------------------------------ transcripts --
async def test_interim_results_are_lossy_partials_including_stable_segments() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 100)

    connector.current.push(fixture("interim_hinglish"))
    connector.current.push(fixture("final_hinglish"))
    connector.current.push(results("और", 1.6, 0.3, is_final=False))
    await settle()
    await stt.close()
    partials = of(await drain(stt), SttPartial)

    assert [p.text for p in partials] == ["मेरा नाम", "मेरा नाम Arun है।", "मेरा नाम Arun है। और"]
    assert [p.revision for p in partials] == [1, 2, 3]


async def test_finalize_emits_turn_result_after_from_finalize_in_audio_time_order() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 300)  # 6 s of audio
    connector.current.push(fixture("final_numbers"))  # 1.6-4.0 s
    connector.current.push(fixture("final_hinglish"))  # 0.0-1.6 s, arrives later
    await settle()

    await stt.finalize_turn(stamp(TURN_1), AudioWindow(start_ms=0, end_ms=6000))
    connector.current.push(fixture("final_names_from_finalize"))  # 4.0-5.5 s
    await settle()
    await stt.close()
    events = await drain(stt)

    final = of(events, SttTurnFinalized)[0]
    assert connector.current.finalizes == 1
    assert final.text == "मेरा नाम Arun है। My number is 98765 43210. NiaLabs and Schoollog."
    assert not final.finalization_timed_out
    assert final.stamp.turn_id == TURN_1
    segments = of(events, SttFinalSegment)
    assert [s.audio_start_ms for s in segments] == sorted(s.audio_start_ms for s in segments)


async def test_segments_outside_the_window_belong_to_the_neighbouring_turns() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 300)
    connector.current.push(fixture("final_hinglish"))
    connector.current.push(fixture("final_numbers"))
    await settle()

    await stt.finalize_turn(stamp(TURN_1), AudioWindow(start_ms=0, end_ms=1600))
    connector.current.push(fixture("empty_from_finalize"))
    await stt.finalize_turn(stamp(TURN_2), AudioWindow(start_ms=1600, end_ms=4000))
    connector.current.push(fixture("empty_from_finalize"))
    await settle()
    await stt.close()
    finals = {f.stamp.turn_id: f.text for f in of(await drain(stt), SttTurnFinalized)}

    assert finals == {TURN_1: "मेरा नाम Arun है।", TURN_2: "My number is 98765 43210."}


async def test_empty_and_noise_finals_produce_an_unusable_turn_without_language() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 100)
    connector.current.push(fixture("noise_final"))
    connector.current.push(fixture("silence_interim"))

    await stt.finalize_turn(stamp(TURN_1))
    connector.current.push(fixture("empty_from_finalize"))
    await settle()
    await stt.close()
    final = of(await drain(stt), SttTurnFinalized)[0]

    assert not final.is_usable
    assert final.language is None
    assert final.confidence is None


async def test_finalize_timeout_emits_the_assembly_so_far_and_discards_the_late_result() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 100)
    connector.current.push(fixture("final_hinglish"))
    await settle()

    await stt.finalize_turn(stamp(TURN_1))
    await asyncio.sleep(0.15)
    connector.current.push(fixture("final_names_from_finalize"))  # late: after the timeout
    await settle()
    await stt.close()
    events = await drain(stt)

    finals = of(events, SttTurnFinalized)
    assert len(finals) == 1
    assert finals[0].finalization_timed_out
    assert finals[0].text == "मेरा नाम Arun है।"
    assert [w.code for w in of(events, SttWarning)] == ["finalization_timeout"]
    assert of(events, SttStreamClosed)[-1].counters["late_finalize_results"] == 1


async def test_cancelled_turn_results_never_surface() -> None:
    connector = FakeDeepgramConnector(["ignored words"])
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 50)

    await stt.finalize_turn(stamp(TURN_1))
    await stt.cancel_turn(TURN_1)
    await settle()
    await stt.finalize_turn(stamp(TURN_1))
    await stt.close()
    events = await drain(stt)

    assert of(events, SttTurnFinalized) == []
    assert of(events, SttFinalSegment) == []


async def test_provider_speech_and_endpoint_signals_are_advisory_counters_only() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 100)

    connector.current.push(fixture("speech_started"))
    connector.current.push(fixture("final_hinglish"))  # speech_final=true
    connector.current.push(fixture("utterance_end"))
    connector.current.push(fixture("unknown"))
    await settle()
    await stt.close()
    events = await drain(stt)

    assert of(events, SttTurnFinalized) == []
    assert of(events, SttWarning) == []
    assert connector.current.finalizes == 0
    counters = of(events, SttStreamClosed)[-1].counters
    assert counters["provider_speech_started"] == 1
    assert counters["provider_speech_final"] == 1
    assert counters["provider_utterance_end"] == 1
    assert counters["unknown_messages"] == 1


async def test_malformed_messages_warn_up_to_the_per_stream_cap() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    await stt.start(SttStreamConfig())

    for _ in range(8):
        connector.current.push(fixture("malformed_results"))
    await settle()
    await stt.close()
    events = await drain(stt)

    assert len(of(events, SttWarning)) == 5
    counters = of(events, SttStreamClosed)[-1].counters
    assert counters["malformed_messages"] == 8
    assert counters["warnings_suppressed"] == 3


async def test_keepalive_is_sent_only_while_no_audio_flows() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)
    await stt.start(SttStreamConfig())

    await asyncio.sleep(0.12)
    await stt.close()

    assert connector.current.keepalives >= 1


# ------------------------------------------------------- failure & retry --
async def test_transient_connect_failures_retry_as_distinct_attempts() -> None:
    connector = FakeDeepgramConnector(
        connect_errors=[
            DeepgramTransportError(DeepgramErrorKind.UNAVAILABLE, 503),
            DeepgramTransportError(DeepgramErrorKind.RATE_LIMITED, 429),
        ]
    )
    stt = adapter(connector)

    await stt.start(SttStreamConfig())
    await stt.close()
    events = await drain(stt)

    failed = [c for c in of(events, SttStreamClosed) if c.outcome is SttStreamOutcome.FAILED]
    started = of(events, SttStreamStarted)[0]
    assert [c.attempt.attempt_number for c in failed] == [1, 2]
    assert started.attempt.attempt_number == 3
    assert started.attempt.previous_attempt_operation_id == failed[1].stamp.operation_id
    assert len({c.stamp.operation_id for c in [*failed, started]}) == 3
    assert {c.attempt.logical_request_id for c in failed} == {started.attempt.logical_request_id}
    assert failed[0].usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS) == Decimal(0)


async def test_authentication_failure_is_not_retried() -> None:
    connector = FakeDeepgramConnector(
        connect_errors=[DeepgramTransportError(DeepgramErrorKind.AUTHENTICATION, 401)]
    )
    stt = adapter(connector)

    with pytest.raises(NormalizedFailureError) as raised:
        await stt.start(SttStreamConfig())

    assert raised.value.failure.error_type is ErrorType.AUTHENTICATION_FAILED
    assert connector.connect_calls == 1


async def test_connect_timeout_exhausts_three_attempts() -> None:
    connector = FakeDeepgramConnector(hang_connects=3)
    stt = adapter(connector)

    with pytest.raises(NormalizedFailureError) as raised:
        await stt.start(SttStreamConfig())

    assert raised.value.failure.error_type is ErrorType.CONNECTION_FAILED
    assert raised.value.failure.failure_phase == "connect_timeout"
    assert connector.connect_calls == RetryPolicy().maximum_attempts


async def test_unapproved_keyterms_fail_the_start_without_connecting() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector)

    with pytest.raises(NormalizedFailureError) as raised:
        await stt.start(SttStreamConfig(keyterms=("NiaLabs",)))

    assert raised.value.failure.error_type is ErrorType.CONFIGURATION_INVALID
    assert raised.value.failure.failure_phase == "keyterms_not_approved"
    assert connector.connect_calls == 0


async def test_approved_keyterms_are_mapped_for_a_mocked_stream() -> None:
    connector = FakeDeepgramConnector()
    stt = adapter(connector, keyterms_approved=True)

    await stt.start(SttStreamConfig(keyterms=("NiaLabs", "Schoollog")))
    await stt.close()

    assert connector.params[0]["keyterm"] == ("NiaLabs", "Schoollog")
    assert of(await drain(stt), SttStreamStarted)[0].keyterm_count == 2


async def test_stream_loss_reconnects_fails_the_unconfirmed_turn_and_keeps_later_turns() -> None:
    connector = FakeDeepgramConnector(["second turn"])
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 50)  # 0-1000 ms sent, never confirmed
    first = connector.current

    first.drop()
    await settle()
    await write(stt, 50, 5)  # sent to the new stream (or buffered and replayed)
    await settle()
    await stt.finalize_turn(stamp(TURN_1), AudioWindow(start_ms=0, end_ms=900))
    await stt.finalize_turn(stamp(TURN_2), AudioWindow(start_ms=1000, end_ms=1100))
    await settle()
    await stt.close()
    events = await drain(stt)

    assert len(connector.connections) == 2
    assert connector.current.audio_bytes == 5 * 640
    failed_turns = [f for f in of(events, SttFailed) if f.stamp.turn_id == TURN_1]
    assert failed_turns
    assert failed_turns[0].failure.user_affected
    finals = {f.stamp.turn_id: f.text for f in of(events, SttTurnFinalized)}
    assert finals == {TURN_2: "second turn"}
    outcomes = [c.outcome for c in of(events, SttStreamClosed)]
    assert outcomes == [SttStreamOutcome.FAILED, SttStreamOutcome.SUCCEEDED]


async def test_audio_during_recovery_is_buffered_bounded_and_replayed() -> None:
    connector = FakeDeepgramConnector(
        connect_errors=[DeepgramTransportError(DeepgramErrorKind.UNAVAILABLE, 503)]
    )
    stt = adapter(connector, retry_delay_ms=lambda _attempt: 0)
    connector._errors = []  # the initial connect succeeds
    await stt.start(SttStreamConfig())
    connector._errors = [DeepgramTransportError(DeepgramErrorKind.UNAVAILABLE, 503)]
    connector.fail_sends = True

    await write(stt, 0, 1)  # send fails -> recovery; frame kept for replay
    connector.fail_sends = False
    await write(stt, 1, 14)  # 15 frames = 300 ms > 200 ms buffer while recovering
    await asyncio.sleep(0.05)
    await stt.close()
    events = await drain(stt)

    # 15 frames arrived while recovering; the oldest 100 ms overflowed the 200 ms buffer.
    assert len(connector.connections) == 2
    assert connector.current.audio_bytes == 10 * 640
    assert "inbound_audio_overflow" in [w.code for w in of(events, SttWarning)]
    assert stt.counters["audio_overflow_ms"] == 100


async def test_unrecoverable_stream_fails_session_and_pending_turns() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    stt = adapter(connector)
    await stt.start(SttStreamConfig())
    await write(stt, 0, 10)
    connector._errors = [
        DeepgramTransportError(DeepgramErrorKind.AUTHENTICATION, 401),
    ]

    await stt.finalize_turn(stamp(TURN_1), AudioWindow(start_ms=0, end_ms=150))
    connector.current.drop()
    await settle()
    await stt.write_audio(frame(20), stamp())
    await stt.finalize_turn(stamp(TURN_2), AudioWindow(start_ms=700, end_ms=800))
    await stt.close()
    events = await drain(stt)

    session_failures = [f for f in of(events, SttFailed) if f.stamp.turn_id is None]
    assert session_failures[0].failure.error_type is ErrorType.AUTHENTICATION_FAILED
    assert {f.stamp.turn_id for f in of(events, SttFailed)} >= {TURN_1, TURN_2}
    assert of(events, SttTurnFinalized) == []


async def test_silent_provider_triggers_the_response_watchdog_and_a_new_attempt() -> None:
    connector = FakeDeepgramConnector()
    timing = DeepgramTiming(
        connect_timeout_s=0.05,
        close_timeout_s=0.2,
        maintenance_interval_s=0.01,
        response_timeout_ms=40,
    )
    stt = adapter(connector, timing=timing)
    await stt.start(SttStreamConfig())

    await write(stt, 0, 3)
    await asyncio.sleep(0.12)
    await stt.close()
    events = await drain(stt)

    assert len(connector.connections) >= 2
    first_closed = of(events, SttStreamClosed)[0]
    assert first_closed.outcome is SttStreamOutcome.FAILED
    assert first_closed.counters["response_timeouts"] == 1
