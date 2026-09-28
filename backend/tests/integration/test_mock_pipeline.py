"""Full in-process mock pipeline (docs/14 §8 exit gate).

synthetic audio -> speech activity -> mock STT final -> mock conversation
stream -> segmentation -> mock TTS -> playback acknowledgement -> session
finalization, plus interruption mid-playback in the canonical order.
All ordering is by events and gates; no wall-clock sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from voice_agent.contracts.enums import (
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    ResponseCompletionStatus,
    SessionStatus,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorCategory, ErrorType
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackAck, PlaybackAckKind
from voice_agent.conversation_adapters.mock.adapter import PAUSE_UNTIL_CANCELLED, MockReply
from voice_agent.transport_adapters.mock.adapter import (
    MockTransport,
    Signal,
    Silence,
    Speak,
    WaitForEvent,
    WaitUntil,
)

TIMEOUT_S = 10


def _turn_event_sent(event: str, count: int = 1) -> WaitUntil:
    return WaitUntil(f"{event}>={count}", lambda t: t.sent_count(event) >= count)


def _started_segments(count: int) -> WaitUntil:
    def condition(transport: MockTransport) -> bool:
        return transport.started_segment_count >= count

    return WaitUntil(f"segments_started>={count}", condition)


def _is_subsequence(items: Sequence[str], sequence: Sequence[str]) -> bool:
    position = 0
    for entry in sequence:
        if position < len(items) and entry.startswith(items[position]):
            position += 1
    return position == len(items)


async def _run(harness: Any) -> Any:
    return await asyncio.wait_for(harness.orchestrator.run(), timeout=TIMEOUT_S)


@pytest.mark.asyncio
async def test_one_hindi_turn_runs_end_to_end_and_finalizes(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=["नमस्ते, आप कैसे हैं?"],
        replies=[MockReply(steps=("नमस्ते! ", "मैं ठीक हूँ। ", "आप बताइए", ", मैं कैसे help करूँ?"))],
    )

    session = await _run(harness)

    assert session.status is SessionStatus.ENDED
    report = harness.orchestrator.report
    (turn,) = report.turns
    assert turn.status is TurnStatus.COMPLETED
    assert turn.response_completion_status is ResponseCompletionStatus.COMPLETED
    assert turn.final_transcript == "नमस्ते, आप कैसे हैं?"
    assert turn.generated_text == "नमस्ते! मैं ठीक हूँ। आप बताइए, मैं कैसे help करूँ?"
    assert turn.spoken_text == "नमस्ते! मैं ठीक हूँ। आप बताइए, मैं कैसे help करूँ?"
    assert turn.spoken_text_accuracy is SpokenTextAccuracy.CONFIRMED
    assert [m.role.value for m in report.history] == ["user", "assistant"]
    assert len(harness.transport.published) == 3 * 3
    assert {f.identity.cancellation_generation for f in harness.transport.published} == {0}
    assert harness.tts.requests[0].language_code.value == "hi-IN"


@pytest.mark.asyncio
async def test_final_transcript_is_durable_before_generation_starts(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi! How can I help?",))],
    )

    await _run(harness)

    assert _is_subsequence(
        ["turn_repository.save:transcript_final", "conversation.stream"], harness.log
    )


@pytest.mark.asyncio
async def test_durable_events_are_sequenced_and_ordered(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi. ", "How can I help?"))],
    )

    await _run(harness)

    durable = await harness.durable()
    assert [event.sequence_number for event in durable] == list(range(1, len(durable) + 1))
    types = [event.event_type.value for event in durable]
    conversation_chain = [
        "session.active",
        "turn.opened",
        "user.speech_started",
        "user.speech_ended",
        "stt.turn_finalized",
        "conversation.started",
        "conversation.first_token",
        "conversation.segment_ready",
        "conversation.completed",
        "turn.completed",
        "session.ending",
        "cost.calculated",
        "session.ended",
    ]
    audio_chain = [
        "conversation.segment_ready",
        "tts.segment_started",
        "tts.first_audio",
        "playback.started",
        "playback.completed",
        "turn.completed",
    ]
    assert _is_subsequence(conversation_chain, types)
    assert _is_subsequence(audio_chain, types)
    assert EventType.CONVERSATION_TEXT_DELTA not in {e.event_type for e in durable}
    assert all("transcript" not in event.payload for event in durable)


@pytest.mark.asyncio
async def test_session_cost_is_calculated_from_attempt_usage(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi! How can I help?",), input_tokens=1000)],
    )

    await _run(harness)

    calculation = harness.orchestrator.report.cost
    assert calculation is not None
    assert calculation.status.value == "final"
    assert calculation.reporting_currency.value == "USD"
    gross = {line.usage_unit.value: line.gross_cost for line in calculation.lines}
    usd = {line.usage_unit.value: line.converted_cost for line in calculation.lines}
    assert gross["input_tokens"] == 1, "original INR amount is preserved"
    assert gross["output_tokens"] == Decimal("0.05")
    assert usd["input_tokens"] == Decimal("0.01")
    assert usd["output_tokens"] == Decimal("0.0005")
    assert len(harness.costs.calculations) == 1


@pytest.mark.asyncio
async def test_interruption_mid_playback_follows_canonical_order(build_harness: Any) -> None:
    harness = _interruption_harness(build_harness)

    session = await _run(harness)

    assert session.status is SessionStatus.ENDED
    assert _is_subsequence(
        [
            "fence.advanced:1",
            "conversation.cancel",
            "tts.cancel_segment",
            "transport.clear_playback",
            "transport.send_event:turn.interrupted",
        ],
        harness.log,
    )
    durable = await harness.durable()
    types = [e.event_type.value for e in durable]
    assert _is_subsequence(
        [
            "turn.interruption_detected",
            "conversation.cancelled",
            "tts.cancelled",
            "tts.cancelled",
            "playback.cancelled",
            "turn.interrupted",
            "turn.opened",
        ],
        types,
    )
    cancelled = [
        e.payload["segment_sequence"] for e in durable if e.event_type is EventType.TTS_CANCELLED
    ]
    assert cancelled == [2, 1], "queued segments are cancelled before the active segment"


@pytest.mark.asyncio
async def test_interrupted_turn_keeps_only_delivered_text(build_harness: Any) -> None:
    harness = _interruption_harness(build_harness)

    await _run(harness)

    first, second = harness.orchestrator.report.turns
    assert first.status is TurnStatus.INTERRUPTED
    assert first.interruption.reason is InterruptionReason.USER_BARGE_IN
    assert first.interruption.phase is InterruptionPhase.SPEAKING
    assert first.response_completion_status is ResponseCompletionStatus.INTERRUPTED
    assert first.spoken_text == "First sentence here."
    assert first.spoken_text_accuracy is SpokenTextAccuracy.ESTIMATED
    assert "Late" not in first.generated_text
    assert first.synthesized_text == "First sentence here. Second sentence here."
    assert second.status is TurnStatus.COMPLETED
    assert second.final_transcript == "second question"
    second_request = harness.conversation.requests[1]
    assert [(m.role.value, m.text) for m in second_request.history] == [
        ("user", "pehla sawaal"),
        ("assistant", "First sentence here."),
    ]


@pytest.mark.asyncio
async def test_stale_generation_cannot_publish_llm_tts_or_playback_output(
    build_harness: Any,
) -> None:
    harness = _interruption_harness(build_harness)

    await _run(harness)

    transport = harness.transport
    cut = transport.clear_marks[0]
    assert all(f.identity.cancellation_generation >= 1 for f in transport.published[cut:])
    second_segment = harness.tts.requests[1].segment_id
    assert sum(1 for f in transport.published if f.identity.segment_id == second_segment) == 1
    assert [r.text for r in harness.tts.requests].count("Late sentence never spoken.") == 0
    late = harness.orchestrator.report.late_discards
    assert late.get("conversation", 0) >= 1
    assert late.get("tts", 0) >= 1


def _interruption_harness(build_harness: Any) -> Any:
    return build_harness(
        script=[
            Speak(300),
            Silence(800),
            _started_segments(2),
            Speak(400, amplitude=0.8),
            Silence(800),
            _turn_event_sent("turn.completed"),
        ],
        transcripts=["pehla sawaal", "second question"],
        replies=[
            MockReply(
                steps=(
                    "First sentence here. ",
                    "Second sentence here. ",
                    "Third sentence queued. ",
                    PAUSE_UNTIL_CANCELLED,
                    "Late sentence never spoken. ",
                ),
                honor_cancel=False,
            ),
            MockReply(steps=("Sure, here is the answer.",)),
        ],
        tts_pause_when=lambda request: request.text.startswith("Second"),
        tts_honor_cancel=False,
    )


@pytest.mark.asyncio
async def test_short_candidate_is_suppressed_and_playback_continues(build_harness: Any) -> None:
    release = asyncio.Event()
    harness = build_harness(
        script=[
            Speak(300),
            Silence(800),
            _started_segments(1),
            Speak(100, amplitude=0.8),
            Silence(600),
            Signal("release_llm", release.set),
            _turn_event_sent("turn.completed"),
        ],
        transcripts=["ek sawaal"],
        replies=[MockReply(steps=("Pehla jawab. ", release, "Doosra jawab."))],
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.COMPLETED
    assert turn.interruption.false_interruption_suppressed_count == 1
    assert turn.interruption.detected_count == 1
    assert not turn.interruption.accepted
    assert EventType.TURN_FALSE_INTERRUPTION_SUPPRESSED in harness.event_types()
    assert harness.transport.clear_count == 1, "only finalization clears playback"


@pytest.mark.asyncio
async def test_empty_transcript_is_discarded_without_llm_request(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.discarded")],
        transcripts=["   "],
        replies=[],
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.DISCARDED
    assert turn.input_disposition is InputDisposition.EMPTY
    assert harness.conversation.requests == []


@pytest.mark.asyncio
async def test_empty_transcript_with_fallback_speaks_clarification(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=[""],
        replies=[],
        clarification_fallback=True,
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.COMPLETED
    assert turn.fallback_used
    assert turn.input_disposition is InputDisposition.EMPTY
    assert harness.conversation.requests == []
    assert harness.tts.requests[0].text.startswith("Sorry")
    assert harness.orchestrator.report.history == (), "fallback-only turns stay out of history"


@pytest.mark.asyncio
async def test_stale_or_forged_playback_ack_is_ignored(build_harness: Any) -> None:
    holder: dict[str, Any] = {}

    def forge() -> None:
        forged = PlaybackAck(
            ack=PlaybackAckKind.COMPLETED,
            identity=PlaybackAckIdentity(
                worker_generation=9,
                cancellation_generation=0,
                segment_id="00000000-0000-4000-8000-0000000000ff",
            ),
        )
        holder["harness"].transport.inject_client_event(forged)

    harness = build_harness(
        script=[
            Speak(300),
            Silence(800),
            _turn_event_sent("turn.completed"),
            Signal("forge_ack", forge),
            Speak(20),
        ],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi! How can I help?",))],
    )
    holder["harness"] = harness

    await _run(harness)

    assert harness.orchestrator.report.ignored_acks >= 1
    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_truncated_response_discards_incomplete_tail(build_harness: Any) -> None:
    from voice_agent.contracts.enums import FinishReason

    harness = build_harness(
        script=[Speak(300), Silence(800), _turn_event_sent("turn.completed")],
        transcripts=["Tell me a story"],
        replies=[
            MockReply(
                steps=("Once upon a time. ", "There was a"),
                finish_reason=FinishReason.MAXIMUM_TOKENS,
            )
        ],
    )

    await _run(harness)

    (turn,) = harness.orchestrator.report.turns
    assert turn.status is TurnStatus.COMPLETED
    assert turn.response_completion_status is ResponseCompletionStatus.TRUNCATED_PARTIAL
    assert turn.spoken_text == "Once upon a time."
    assert [r.text for r in harness.tts.requests] == ["Once upon a time."]


@pytest.mark.asyncio
async def test_inbound_audio_overflow_records_realtime_overload(build_harness: Any) -> None:
    harness = build_harness(
        script=[Speak(2400)],
        transcripts=[],
        replies=[],
        yield_per_frame=False,
    )

    session = await _run(harness)

    failures = list(harness.errors.errors.values())
    assert any(f.error_type is ErrorType.REALTIME_OVERLOAD for f in failures)
    assert all(
        f.category is ErrorCategory.CAPACITY
        for f in failures
        if f.error_type is ErrorType.REALTIME_OVERLOAD
    )
    assert session.is_terminal


@pytest.mark.asyncio
async def test_barge_in_while_thinking_cancels_the_response_and_opens_the_next_turn(
    build_harness: Any,
) -> None:
    """Decision 069: committed turn, LLM streaming, no agent audio yet; 0.5 threshold."""
    llm_started = asyncio.Event()
    harness = build_harness(
        script=[
            Speak(300),
            Silence(800),
            WaitForEvent("llm_streaming", llm_started),
            Speak(400, amplitude=0.6),
            Silence(800),
            _turn_event_sent("turn.completed"),
        ],
        transcripts=["pehla sawaal", "doosra sawaal"],
        replies=[
            MockReply(
                steps=("Soch raha hoon", PAUSE_UNTIL_CANCELLED, " late text."),
                honor_cancel=False,
            ),
            MockReply(steps=("Doosra jawab.",)),
        ],
    )
    harness.conversation.on_stream_started(llm_started.set)

    await _run(harness)

    first, second = harness.orchestrator.report.turns
    assert first.status is TurnStatus.INTERRUPTED
    assert first.interruption.phase is InterruptionPhase.THINKING
    assert first.spoken_text == ""
    assert "late" not in first.generated_text
    assert second.status is TurnStatus.COMPLETED
    assert second.final_transcript == "doosra sawaal"
    assert _is_subsequence(
        [
            "conversation.stream",
            "fence.advanced:1",
            "conversation.cancel",
            "transport.clear_playback",
            "transport.send_event:turn.interrupted",
            "conversation.stream",
        ],
        harness.log,
    )
    assert {f.identity.cancellation_generation for f in harness.transport.published} == {1}
    assert harness.orchestrator.report.late_discards.get("conversation", 0) >= 1
