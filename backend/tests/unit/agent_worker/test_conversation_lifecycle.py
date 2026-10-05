"""WP10 conversation orchestrator: greeting, clarification, timeouts, absence, failures, shutdown.

Offline over the same rig as ``test_conversation_orchestrator`` (no network, no key).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.support.conversation_rig import FAR, SILENCE, SPEECH, build
from tests.support.fake_deepgram import FakeDeepgramConnector, results
from tests.support.fake_openai import failed, reply

from voice_agent.agent_worker.conversation_policy import ConversationTimeouts
from voice_agent.contracts.enums import (
    DisconnectReason,
    InputDisposition,
    InterruptionReason,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import (
    ClientReady,
    PlaybackAck,
    PlaybackAckKind,
    TransportEvent,
    TransportEventKind,
)
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter
from voice_agent.turn_management.fallbacks import (
    RESPONSE_FAILED,
    SESSION_TIME_LIMIT,
    UNCLEAR_INPUT,
)
from voice_agent.turn_management.greeting import OPENING_GREETING

pytestmark = pytest.mark.asyncio
DOCS10_GREETING = (
    "नमस्ते! मैं एक AI voice assistant हूँ। आप Hindi, Hinglish या English में बात कर सकते हैं। "
    "मैं आपकी किस तरह help करूँ?"
)


def _texts(tts: MockTtsAdapter) -> list[str]:
    return [request.text for request in tts.requests]


def _event(kind: TransportEventKind) -> TransportEvent:
    return TransportEvent(kind=kind, at_ms=0)


async def test_greeting_text_is_the_approved_docs10_phrase() -> None:
    assert OPENING_GREETING.text == DOCS10_GREETING
    assert OPENING_GREETING.template_id == "greeting.opening.v1"


async def test_greeting_plays_exactly_once_across_duplicate_ready_and_reconnect() -> None:
    tts = MockTtsAdapter(frames_per_segment=2)
    rig = build(tts=tts, greeting=True)
    await rig.start()

    rig.transport.client(ClientReady())
    rig.transport.client(ClientReady())
    await rig.until(lambda: bool(rig.finals()))
    await rig.idle()
    rig.orchestrator.on_transport_event(_event(TransportEventKind.BROWSER_LEFT))
    rig.orchestrator.on_transport_event(_event(TransportEventKind.RECONNECTED))
    rig.transport.client(ClientReady())
    await asyncio.sleep(0.05)
    await rig.stop()

    spoken = " ".join(_texts(tts))
    assert spoken.count("नमस्ते") == 1
    assert rig.llm.params == []  # no LLM request for the greeting
    [greeting] = await rig.all_turns()
    assert greeting.status is TurnStatus.COMPLETED
    assert greeting.fallback_used
    assert greeting.input_disposition is InputDisposition.EMPTY
    [final] = rig.finals()
    assert final["payload"]["fallback_template_id"] == "greeting.opening.v1"
    assert rig.orchestrator.counters["greetings"] == 1
    assert rig.orchestrator.counters["duplicate_client_ready"] == 2


async def test_recovered_worker_never_replays_the_greeting() -> None:
    tts = MockTtsAdapter()
    rig = build(tts=tts, greeting=False)
    await rig.start()

    rig.transport.client(ClientReady())
    await asyncio.sleep(0.05)
    await rig.stop()

    assert tts.requests == []
    assert await rig.all_turns() == []


async def test_the_greeting_can_be_interrupted_and_never_resumes() -> None:
    tts = MockTtsAdapter(frames_per_segment=4, pause_when=lambda r: "नमस्ते" in r.text)
    rig = build([reply("Haan, boliye.")], ["Mujhe help chahiye"], tts=tts, greeting=True)
    await rig.start()

    rig.transport.client(ClientReady())
    await rig.until(lambda: bool(rig.transport.published))
    await rig.feed(15, SPEECH)
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    greeting, turn = await rig.all_turns()
    assert greeting.status is TurnStatus.INTERRUPTED
    assert greeting.interruption.interruption_latency_ms is not None
    assert turn.status is TurnStatus.COMPLETED
    assert sum("नमस्ते" in text for text in _texts(tts)) == 1


async def test_empty_transcript_after_barge_in_speaks_the_clarification_once() -> None:
    tts = MockTtsAdapter(frames_per_segment=3, pause_when=lambda r: "lamba" in r.text)
    rig = build([reply("Yeh lamba jawab hai.")], ["Batao", ""], tts=tts)
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: bool(rig.transport.published))
    old_generation = rig.transport.published[0].identity.cancellation_generation
    await rig.feed(15, SPEECH)
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.utterance()  # a later plain noise turn is discarded, not clarified again
    await rig.idle()
    await rig.stop()

    old, clarified, noise = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert clarified.status is TurnStatus.COMPLETED
    assert clarified.fallback_used
    assert clarified.input_disposition is InputDisposition.EMPTY
    assert noise.status is TurnStatus.DISCARDED
    assert len(rig.llm.params) == 1  # the clarification made no LLM request
    assert sum(UNCLEAR_INPUT.text.startswith(t) or t in UNCLEAR_INPUT.text for t in _texts(tts))
    stale = [
        f
        for f in rig.transport.published
        if f.identity.cancellation_generation == old_generation and f.turn_id == old.turn_id
    ]
    assert len(stale) == 1  # the interrupted audio never resumed
    fallback_ids = [f["payload"].get("fallback_template_id") for f in rig.finals()]
    assert fallback_ids.count(UNCLEAR_INPUT.template_id) == 1


async def test_stt_finalization_timeout_uses_the_clarification_fallback() -> None:
    connector = FakeDeepgramConnector(answer_finalize=False)
    tts = MockTtsAdapter(frames_per_segment=2)
    rig = build(tts=tts, deepgram=connector, finalize_timeout_ms=100)
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: bool(rig.finals()))
    await rig.idle()
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.input_disposition is InputDisposition.TIMED_OUT
    assert turn.fallback_used
    assert rig.llm.params == []
    assert rig.finals()[0]["payload"]["fallback_template_id"] == UNCLEAR_INPUT.template_id


async def test_speech_resumed_before_the_transcript_is_carried_into_the_next_turn() -> None:
    connector = FakeDeepgramConnector(["aage ki baat"], answer_finalize=False)
    rig = build([reply("Samajh gayi.")], deepgram=connector, finalize_timeout_ms=400)
    await rig.start()

    await rig.feed(20, SPEECH)
    connector.current.push(results("Mujhe ek baat", 0.0, 0.4, is_final=True))
    await rig.feed(36, SILENCE)  # 720 ms: committed, transcript still pending
    assert rig.orchestrator._awaiting
    await rig.feed(15, SPEECH)  # the user continues the same thought
    await asyncio.sleep(0.45)  # the first finalization times out with its text
    connector.answer_finalize = True
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    first, second = await rig.all_turns()
    assert first.status is TurnStatus.INTERRUPTED
    assert second.status is TurnStatus.COMPLETED
    assert second.final_transcript is not None
    assert second.final_transcript.startswith("Mujhe ek baat")
    assert len(rig.llm.params) == 1
    assert rig.orchestrator.counters["carried_transcripts"] == 1


async def test_duplicate_acks_and_late_transcripts_are_ignored() -> None:
    rig = build([reply("Theek.")], ["Hello"])
    await rig.start()

    await rig.utterance()
    await rig.idle()
    stray = PlaybackAckIdentity(
        worker_generation=1,
        cancellation_generation=99,
        segment_id="00000000-0000-4000-8000-0000000000aa",
    )
    for _ in range(3):
        rig.transport.client(PlaybackAck(ack=PlaybackAckKind.COMPLETED, identity=stray))
    await asyncio.sleep(0.05)
    [turn] = await rig.all_turns()
    assert await rig.gate.authorize(turn) is False  # a terminal turn never re-generates
    await rig.stop()

    assert rig.gate.ignored_acks == 3
    assert len(rig.llm.params) == 1


async def test_browser_absence_cancels_output_and_pauses_new_turns() -> None:
    tts = MockTtsAdapter(frames_per_segment=4, pause_when=lambda r: "lamba" in r.text)
    llm = [reply("Yeh lamba jawab."), reply("Wapas aa gaye.")]
    rig = build(llm, ["Batao", "Main wapas"], tts=tts)
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: bool(rig.transport.published))
    rig.orchestrator.on_transport_event(_event(TransportEventKind.BROWSER_LEFT))
    await rig.until(lambda: rig.orchestrator._paused)
    await rig.idle()
    await rig.utterance()  # nothing is heard while the browser is absent
    assert len(await rig.all_turns()) == 1
    rig.orchestrator.on_transport_event(_event(TransportEventKind.RECONNECTED))
    await rig.until(lambda: not rig.orchestrator._paused)
    await rig.utterance()
    await rig.idle()
    await rig.stop()

    old, new = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert old.interruption.reason is InterruptionReason.SYSTEM_CANCEL
    assert new.status is TurnStatus.COMPLETED
    assert "recovering" in rig.states()
    assert rig.transport.clears >= 1


async def test_rate_limited_llm_retries_then_speaks_the_failure_fallback() -> None:
    limited = [failed("rate_limit_exceeded")]
    tts = MockTtsAdapter(frames_per_segment=2)
    rig = build([limited, limited, limited, reply("Ab theek.")], ["Pehle", "Phir"], tts=tts)
    await rig.start()

    await rig.utterance()
    await rig.idle()
    await rig.utterance()
    await rig.idle()
    await rig.stop()

    first, second = await rig.all_turns()
    assert first.status is TurnStatus.FAILED
    assert first.fallback_used
    assert second.status is TurnStatus.COMPLETED
    assert len(rig.llm.params) == 4  # three bounded attempts, then the next turn
    assert rig.finals()[0]["payload"]["fallback_template_id"] == RESPONSE_FAILED.template_id


async def test_persistence_degradation_never_blocks_the_conversation() -> None:
    rig = build([reply("Phir bhi chal raha hai.")], ["Hello"])
    rig.events.fail = True
    await rig.start()

    await rig.utterance()
    await rig.idle()
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.status is TurnStatus.COMPLETED
    assert rig.transport.published


async def test_shutdown_mid_response_interrupts_and_closes_providers() -> None:
    tts = MockTtsAdapter(frames_per_segment=4, pause_when=lambda r: True)
    rig = build([reply("Bahut lamba jawab.")], ["Batao"], tts=tts)
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: bool(rig.transport.published))
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.status is TurnStatus.INTERRUPTED
    assert turn.interruption.reason is InterruptionReason.SESSION_END
    assert tts.closed
    assert len(rig.transport.published) == 1


async def test_silence_publishes_idle_and_idle_timeout_requests_the_end() -> None:
    timeouts = ConversationTimeouts(
        maximum_silence_ms=50,
        maximum_user_turn_ms=FAR,
        idle_session_ms=200,
        maximum_duration_deadline_at=None,
        tick_s=0.01,
    )
    rig = build(timeouts=timeouts)
    await rig.start()

    await rig.until(lambda: bool(rig.ended), timeout_s=2)
    await rig.stop()

    assert "idle" in rig.states()
    assert rig.ended == [DisconnectReason.IDLE_TIMEOUT]


async def test_maximum_user_turn_commits_a_never_ending_utterance() -> None:
    timeouts = ConversationTimeouts(
        maximum_silence_ms=FAR,
        maximum_user_turn_ms=100,
        idle_session_ms=FAR,
        maximum_duration_deadline_at=None,
        tick_s=0.01,
    )
    rig = build([reply("Aapne bahut bola.")], ["Lambi baat"], timeouts=timeouts)
    await rig.start()

    await rig.feed(20, SPEECH)
    await asyncio.sleep(0.2)  # still speaking, no endpoint
    await rig.idle()
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.final_transcript == "Lambi baat"
    assert rig.orchestrator.counters["maximum_user_turn_commits"] == 1


async def test_time_limit_notice_is_spoken_once_then_the_session_ends() -> None:
    tts = MockTtsAdapter(frames_per_segment=2)
    deadline = SystemClock().utc_now() + timedelta(seconds=5)  # inside the 8 s notice window
    rig = build(tts=tts, deadline_at=deadline)
    await rig.start()

    await rig.until(lambda: bool(rig.ended))
    await rig.utterance()  # no new user turn is accepted afterwards
    await rig.stop()

    assert rig.ended == [DisconnectReason.MAXIMUM_DURATION]
    [notice] = await rig.all_turns()
    assert notice.fallback_used
    assert notice.status is TurnStatus.COMPLETED
    assert sum(SESSION_TIME_LIMIT.text.startswith(t) for t in _texts(tts)) >= 1
    assert rig.llm.params == []
    assert rig.events.of(EventType.TURN_OPENED)[0].envelope.payload == {"origin": "time_limit"}
