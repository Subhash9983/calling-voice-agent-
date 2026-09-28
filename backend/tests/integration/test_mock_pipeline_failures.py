"""Failure, crash, and idempotent-finalization paths of the mock pipeline (docs/05 §18, §22)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from voice_agent.contracts.enums import SessionStatus, TurnStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import SttFailed
from voice_agent.contracts.tts import TtsEvent, TtsSegmentRequest
from voice_agent.conversation_adapters.mock.adapter import MockReply
from voice_agent.stt_adapters.mock.adapter import MockSttAdapter
from voice_agent.transport_adapters.mock.adapter import MockTransport, Silence, Speak, WaitUntil
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter

TIMEOUT_S = 10
_NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _sent(event: str) -> WaitUntil:
    return WaitUntil(f"{event}", lambda t: t.sent_count(event) >= 1)


async def _run(harness: Any) -> Any:
    return await asyncio.wait_for(harness.orchestrator.run(), timeout=TIMEOUT_S)


def _one_turn(terminal_event: str) -> list[Any]:
    return [Speak(300), Silence(800), _sent(terminal_event)]


@pytest.mark.asyncio
async def test_conversation_failure_fails_the_turn_and_session_still_ends(
    build_harness: Any,
) -> None:
    harness = build_harness(
        script=_one_turn("turn.failed"),
        transcripts=["Hello there"],
        replies=[MockReply(steps=("partial",), fail_with=ErrorType.PROVIDER_UNAVAILABLE)],
    )

    session = await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.FAILED
    assert session.status is SessionStatus.ENDED
    assert EventType.CONVERSATION_FAILED in harness.event_types()
    assert any(
        f.error_type is ErrorType.PROVIDER_UNAVAILABLE for f in harness.errors.errors.values()
    )
    # docs/05 §13: the accepted user transcript stays; the failed response is excluded.
    assert [(m.role.value, m.text) for m in harness.orchestrator.report.history] == [
        ("user", "Hello there")
    ]


@pytest.mark.asyncio
async def test_tts_failure_before_audio_fails_the_turn(build_harness: Any) -> None:
    harness = build_harness(
        script=_one_turn("turn.failed"),
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi there.",))],
        tts_fail_when=lambda _request: True,
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.FAILED
    assert harness.transport.published == []


@pytest.mark.asyncio
async def test_browser_playback_failure_with_nothing_delivered_fails_the_turn(
    build_harness: Any,
) -> None:
    harness = build_harness(
        script=_one_turn("turn.failed"),
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi there.",))],
        playback_failure=True,
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.FAILED
    assert turn.spoken_text == ""


@pytest.mark.asyncio
async def test_rejected_segment_is_never_spoken_and_turn_completes_without_audio(
    build_harness: Any,
) -> None:
    harness = build_harness(
        script=_one_turn("turn.completed"),
        transcripts=["Give me a link"],
        replies=[MockReply(steps=("Visit https://example.com today.",))],
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.COMPLETED
    assert harness.tts.requests == []
    assert any(f.error_type is ErrorType.CONTENT_REJECTED for f in harness.errors.errors.values())


class _FailingStt(MockSttAdapter):
    async def finalize_turn(self, stamp: GenerationStamp) -> None:
        failure = NormalizedFailure(
            component=ErrorComponent.STT,
            provider="mock_stt",
            error_type=ErrorType.CONNECTION_LOST,
            safe_message="stream lost",
            retryable=True,
            session_id=stamp.session_id,
            turn_id=stamp.turn_id,
            occurred_at=_NOW,
        )
        await self._events.put(SttFailed(stamp=stamp, failure=failure))


@pytest.mark.asyncio
async def test_stt_failure_fails_the_turn(build_harness: Any) -> None:
    harness = build_harness(
        script=_one_turn("turn.failed"),
        transcripts=[],
        replies=[],
        stt=_FailingStt([]),
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.FAILED
    assert harness.conversation.requests == []


class _CrashingTts(MockTtsAdapter):
    async def synthesize(self, request: TtsSegmentRequest) -> AsyncIterator[TtsEvent]:
        raise RuntimeError("adapter bug")
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_worker_crash_finalizes_the_session_as_failed(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), WaitUntil("never", lambda _t: False)],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi.",))],
        tts=_CrashingTts(),
    )

    session = await _run(harness)

    assert session.status is SessionStatus.FAILED
    assert harness.orchestrator.report.turns[0].status is TurnStatus.ABANDONED
    assert any(f.error_type is ErrorType.INTERNAL_ERROR for f in harness.errors.errors.values())
    assert EventType.SESSION_FAILED in harness.event_types()


class _BrokenCloseTransport(MockTransport):
    async def close(self) -> None:
        await super().close()
        raise RuntimeError("close failed")


@pytest.mark.asyncio
async def test_finalization_continues_when_a_close_step_fails_and_is_idempotent(
    build_harness: Any,
) -> None:
    harness = build_harness(
        script=_one_turn("turn.completed"),
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi.",))],
        transport_class=_BrokenCloseTransport,
    )

    session = await _run(harness)
    events_before = len(harness.writer.timeline)
    again = await harness.orchestrator.finalize()

    assert session.status is SessionStatus.ENDED
    assert again is session
    assert len(harness.writer.timeline) == events_before
    assert any("transport.close" in f.safe_message for f in harness.errors.errors.values())


@pytest.mark.asyncio
async def test_orchestrator_requires_a_connecting_session(build_harness: Any) -> None:
    from voice_agent.domain.session import VoiceSession
    from voice_agent.orchestration.session_orchestrator import SessionOrchestrator

    harness = build_harness(script=[], transcripts=[], replies=[])
    created = VoiceSession(session_id=harness.orchestrator.session.session_id, correlation_id="c")

    with pytest.raises(ValueError, match="connecting"):
        SessionOrchestrator(session=created, ports=harness.ports, settings=harness.settings)
