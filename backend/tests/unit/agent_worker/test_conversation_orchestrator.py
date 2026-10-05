"""WP10 conversation orchestrator: turn lifecycle, natural barge-in, delivered history.

Offline: synthetic microphone frames drive the real Turn Manager and energy
mock VAD (0.5 / 0.7 thresholds), the real Deepgram, OpenAI, and mock TTS
adapters over scripted fakes. No network, no key.
"""

from __future__ import annotations

import pytest
from tests.support.conversation_rig import NOISE, SILENCE, SPEECH, Rig, build
from tests.support.fake_openai import PAUSE, reply

from voice_agent.contracts.conversation import HistoryRole
from voice_agent.contracts.enums import (
    InterruptionPhase,
    InterruptionReason,
    ResponseCompletionStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.domain.turn import ConversationTurn
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter
from voice_agent.turn_management.turn_manager import TurnPhase

pytestmark = pytest.mark.asyncio


def _by_status(turns: list[ConversationTurn], status: TurnStatus) -> list[ConversationTurn]:
    return [turn for turn in turns if turn.status is status]


async def _speaking(rig: Rig, frames: int = 1) -> None:
    await rig.until(lambda: len(rig.transport.published) >= frames)


async def test_happy_path_speaks_the_reply_and_returns_to_listening() -> None:
    rig = build([reply("Namaste Arun! ", "Aap kaise hain?")], ["Namaste, main Arun hoon."])
    await rig.start()

    await rig.utterance()
    await rig.idle()
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.status is TurnStatus.COMPLETED
    assert turn.final_transcript == "Namaste, main Arun hoon."
    assert turn.spoken_text == "Namaste Arun! Aap kaise hain?"
    assert [m.role for m in rig.gate.history] == [HistoryRole.USER, HistoryRole.ASSISTANT]
    assert rig.gate.history[1].text == "Namaste Arun! Aap kaise hain?"
    assert rig.orchestrator._state.phase is TurnPhase.IDLE
    states = rig.states()
    assert states[0] == "listening"
    assert {"transcribing", "thinking", "speaking"} <= set(states)
    assert states[-1] == "listening"


async def test_rapid_multi_turn_conversation_keeps_delivered_history() -> None:
    rig = build(
        [reply("Pehla jawab."), reply("Doosra jawab."), reply("Teesra jawab.")],
        ["Pehla sawaal", "Doosra sawaal", "Teesra sawaal"],
    )
    await rig.start()

    for _ in range(3):
        await rig.utterance()
        await rig.idle()
    await rig.stop()

    turns = await rig.all_turns()
    assert [t.status for t in turns] == [TurnStatus.COMPLETED] * 3
    assert [m.text for m in rig.gate.history] == [
        "Pehla sawaal",
        "Pehla jawab.",
        "Doosra sawaal",
        "Doosra jawab.",
        "Teesra sawaal",
        "Teesra jawab.",
    ]
    assert len(rig.llm.params) == 3
    third_input = str(rig.llm.params[2]["input"])
    assert "Pehla jawab." in third_input
    assert "Doosra jawab." in third_input


async def test_barge_in_during_first_audio_runs_the_canonical_order() -> None:
    tts = MockTtsAdapter(frames_per_segment=6, pause_when=lambda r: "lamba" in r.text)
    rig = build([reply("Yeh ek lamba jawab hai."), reply("Theek hai.")], ["Batao", "Ruko"], tts=tts)
    tts._record = rig.order.append
    await rig.start()

    await rig.utterance()
    await _speaking(rig)
    old_generation = rig.transport.published[0].identity.cancellation_generation
    rig.order.clear()
    await rig.feed(15, SPEECH)  # 300 ms of continuous speech over agent audio
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    order = rig.order
    assert order.index("llm.cancel") < order.index("tts.cancel_turn")
    assert order.index("tts.cancel_turn") < order.index("transport.clear_queue")
    assert order.index("transport.clear_queue") < order.index("browser.cancelled")
    old, new = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert old.interruption.accepted
    assert old.interruption.reason is InterruptionReason.USER_BARGE_IN
    assert old.interruption.phase is InterruptionPhase.SPEAKING
    assert old.interruption.interruption_latency_ms is not None
    assert old.interruption.accepted_at is not None
    assert old.interruption.playback_stopped_at is not None
    assert old.interruption.playback_stopped_at >= old.interruption.accepted_at
    published = rig.transport.published
    stale = [f for f in published if f.identity.cancellation_generation == old_generation]
    assert len(stale) == 1  # nothing of the old response played after the interruption
    assert new.status is TurnStatus.COMPLETED
    assert new.final_transcript == "Ruko"
    interrupted = rig.events.of(EventType.TURN_INTERRUPTED)
    assert interrupted[0].envelope.payload["interruption_latency_ms"] >= 0
    assert interrupted[0].envelope.payload["interruption_reason"] == "user_barge_in"
    assert interrupted[0].envelope.payload["agent_audio_audible"] is True
    assert "detection_lag_ms" in interrupted[0].envelope.payload


async def test_barge_in_mid_response_keeps_only_heard_text_in_history() -> None:
    tts = MockTtsAdapter(frames_per_segment=3, pause_when=lambda r: "Doosri" in r.text)
    rig = build(
        [reply("Pehli line. ", "Doosri line. ", "Teesri line."), reply("Ok.")],
        ["Sab batao", "Bas"],
        tts=tts,
    )
    await rig.start()

    await rig.utterance()
    await _speaking(rig, frames=4)  # first piece fully played, second started
    await rig.feed(15, SPEECH)
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    old, new = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert old.generated_text == "Pehli line. Doosri line. Teesri line."
    assert "Teesri" not in old.spoken_text
    assert old.response_completion_status is ResponseCompletionStatus.INTERRUPTED
    assistant = [m.text for m in rig.gate.history if m.role is HistoryRole.ASSISTANT]
    assert assistant[0] == "Pehli line. Doosri line."
    assert "Teesri" not in " ".join(m.text for m in rig.gate.history)
    assert new.status is TurnStatus.COMPLETED


async def test_sub_250_ms_noise_during_playback_never_cancels_it() -> None:
    rig = build([reply("Main bol rahi hoon.")], ["Bolo"])
    rig.transport.auto_playout = False
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: rig.transport.playouts >= 1)
    await rig.feed(10, SPEECH)  # 200 ms: a cough/tap
    await rig.feed(30, SILENCE)
    await rig.feed(30, NOISE)  # echo below the 0.7 playback threshold
    await rig.feed(10, SILENCE)
    rig.transport.release_playout()
    await rig.idle()
    await rig.stop()

    [turn] = await rig.all_turns()
    assert turn.status is TurnStatus.COMPLETED
    assert turn.interruption.false_interruption_suppressed_count == 1
    assert turn.interruption.detected_count == 1
    assert not turn.interruption.accepted
    assert rig.transport.clears == 0
    assert "cancelled" not in rig.playback()
    assert len(rig.llm.params) == 1
    assert rig.orchestrator.counters["false_interruptions_suppressed"] == 1
    assert rig.events.of(EventType.TURN_FALSE_INTERRUPTION_SUPPRESSED)


async def test_250_ms_of_continuous_speech_accepts_the_barge_in() -> None:
    rig = build([reply("Main bol rahi hoon."), reply("Haan?")], ["Bolo", "Suno"])
    rig.transport.auto_playout = False
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: rig.transport.playouts >= 1)
    await rig.feed(12, SPEECH)  # 240 ms: suppressed
    await rig.feed(30, SILENCE)
    assert rig.transport.clears == 0
    await rig.feed(13, SPEECH)  # 260 ms: accepted
    await rig.feed(40, SILENCE)
    rig.transport.auto_playout = True
    rig.transport.release_playout()
    await rig.idle()
    await rig.stop()

    old, new = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert old.interruption.false_interruption_suppressed_count == 1
    assert rig.transport.clears >= 1
    assert new.status is TurnStatus.COMPLETED
    assert rig.orchestrator.counters["interruptions_accepted"] == 1


async def test_noise_level_speech_while_thinking_is_a_candidate_at_the_base_threshold() -> None:
    rig = build([[*reply("Soch rahi hoon.")[:1], PAUSE], reply("Naya jawab.")], ["Pehle", "Naya"])
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: rig.gate.responding)
    await rig.feed(15, NOISE)  # 0.6 >= 0.5 while no agent audio plays: real speech
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    old, new = await rig.all_turns()
    assert old.status is TurnStatus.INTERRUPTED
    assert old.interruption.phase is InterruptionPhase.THINKING
    assert new.final_transcript == "Naya"
    assert rig.transport.published  # only the new response was spoken
    assert all(f.turn_id == new.turn_id for f in rig.transport.published)


async def test_speech_already_running_when_the_response_ends_still_opens_a_turn() -> None:
    rig = build([reply("Bas itna."), reply("Haan, aage?")], ["Bolo", "Aur ek baat"])
    rig.transport.auto_playout = False
    await rig.start()

    await rig.utterance()
    await rig.until(lambda: rig.transport.playouts >= 1)
    await rig.feed(5, SPEECH)  # 100 ms: still only a candidate
    rig.transport.release_playout()
    await rig.until(lambda: not rig.gate.responding)
    rig.transport.auto_playout = True
    await rig.feed(20, SPEECH)  # the same speech run continues after the agent finished
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()

    first, second = await rig.all_turns()
    assert first.status is TurnStatus.COMPLETED
    assert second.status is TurnStatus.COMPLETED
    assert second.final_transcript == "Aur ek baat"
    assert rig.orchestrator.counters["resumed_speech_runs"] == 1
