"""TTS-check gate: accepted turn -> GPT-6 Luna -> Bulbul v3 speech -> agent audio (WP9).

Real SpeakingConversationGate + SpeechTurn + OpenAI adapter (scripted fake
Responses stream) + Sarvam adapter (scripted fake seam), real publisher over
the fake session transport (wire encoding included), in-memory repositories.
Offline; no key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from tests.support.fake_openai import FakeResponsesConnector, failed, reply
from tests.support.fake_openai import Step as LlmStep
from tests.support.fake_sarvam import PAUSE, FakeSarvamConnector, Script, audio, error
from tests.support.fake_sarvam import reply as speak
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport

from voice_agent.agent_worker.llm_gate import ConversationSetup
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.agent_worker.tts_gate import SpeakingConversationGate, SpeechConfig
from voice_agent.contracts.enums import (
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
    ResponseCompletionStatus,
    ResponseLanguage,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.realtime_wire import CLIENT_TOPIC
from voice_agent.contracts.transport import PlaybackAck, PlaybackAckKind, RealtimeTopic
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.contracts.usage import UsageUnit
from voice_agent.conversation_adapters.openai.adapter import OpenAiConversationAdapter
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord
from voice_agent.ports.tts import TTSPort
from voice_agent.provider_registry.phase0_prompt import (
    PHASE0_PROMPT_CHECKSUM,
    PHASE0_SYSTEM_INSTRUCTION,
)
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter
from voice_agent.tts_adapters.sarvam.adapter import SarvamTtsAdapter
from voice_agent.turn_management.fallbacks import RESPONSE_FAILED

pytestmark = pytest.mark.asyncio
CONFIG_ID = "00000000-0000-4000-8000-00000000c9a1"
VOICE = TtsVoiceConfig(provider="sarvam", model="bulbul:v3", voice_id="priya")
assert CLIENT_TOPIC  # the browser-to-agent topic the acks below stand in for


@dataclass
class EventLog:
    records: list[EventRecord] = field(default_factory=list)

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()

    def types(self) -> list[EventType]:
        return [r.envelope.event_type for r in self.records]


@dataclass
class CostRuns:
    runs: list[Sequence[CostEntryRecord]] = field(default_factory=list)

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        self.runs.append(entries)


class AckingTransport(FakeSessionTransport):
    """Echoes ``playback.completed`` like the browser does (via the gate hook)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: SpeakingConversationGate | None = None

    async def send_event(self, topic: RealtimeTopic, envelope: Any, *, reliable: bool) -> None:
        await super().send_event(topic, envelope, reliable=reliable)
        payload = envelope.payload
        if topic is RealtimeTopic.PLAYBACK and payload["state"] in {"started", "completed"}:
            identity = PlaybackAckIdentity(
                worker_generation=payload["worker_generation"],
                cancellation_generation=payload["cancellation_generation"],
                segment_id=payload["segment_id"],
            )
            ack = PlaybackAck(ack=PlaybackAckKind(payload["state"]), identity=identity)
            gate = self.gate
            if gate is not None:
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(gate.on_playback_ack(ack))
                )


@dataclass
class Rig:
    gate: SpeakingConversationGate
    llm: FakeResponsesConnector
    sarvam: FakeSarvamConnector
    tts: TTSPort
    transport: FakeSessionTransport
    turns: InMemoryTurnRepository
    operations: InMemoryOperationRepository
    events: EventLog
    costs: CostRuns
    sequence: int = 0

    async def accept(
        self, text: str, language: ResponseLanguage = ResponseLanguage.HINGLISH
    ) -> ConversationTurn:
        self.sequence += 1
        turn = ConversationTurn(
            turn_id=UuidIdGenerator().new_id(), session_id=SESSION_ID, sequence_number=self.sequence
        ).accept_transcript(text, language)
        await self.turns.save(turn)
        return turn

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [s.body for s in self.transport.on(topic)]

    def playback(self) -> list[str]:
        return [b["payload"]["state"] for b in self.on("va.playback.v1")]

    def states(self) -> list[str]:
        return [b["payload"]["state"] for b in self.on("va.state.v1")]

    def finals(self) -> list[dict[str, Any]]:
        return [b for b in self.on("va.response.v1") if b["payload"]["is_final"]]

    async def turn(self, turn_id: str) -> ConversationTurn:
        stored = await self.turns.get(turn_id)
        assert stored is not None
        return stored

    async def tts_operations(self) -> list[ProviderOperation]:
        operations = await self.operations.list_for_session(SESSION_ID)
        return [o for o in operations if o.component is OperationComponent.TTS]


def build(
    llm: Sequence[Sequence[LlmStep]],
    speech: Sequence[Script] = (),
    *,
    tts: TTSPort | None = None,
    transport: FakeSessionTransport | None = None,
    ack_grace_ms: int = 0,
) -> Rig:
    transport = transport or FakeSessionTransport()
    clock, ids = SystemClock(), UuidIdGenerator()
    llm_connector = FakeResponsesConnector(llm)
    sarvam = FakeSarvamConnector.with_scripts(speech)
    adapter = tts or SarvamTtsAdapter(sarvam, clock=clock, prewarm=False, keepalive_s=3600)
    turns, operations = InMemoryTurnRepository(), InMemoryOperationRepository()
    events, costs = EventLog(), CostRuns()
    evidence = SttEvidence(
        EvidenceContext(
            session_id=SESSION_ID,
            correlation_id="wp9-test",
            agent_config_id=CONFIG_ID,
            environment=AgentConfigEnvironment.DEVELOPMENT,
            worker_generation=1,
        ),
        turns=turns,
        operations=operations,
        events=events,
        costs=costs,
        rate_card=phase0_rate_card(),
        clock=clock,
        ids=ids,
    )
    no_wait = RetryPolicy(initial_backoff_ms=0, maximum_backoff_ms=0)
    setup = ConversationSetup(
        session_id=SESSION_ID,
        correlation_id="wp9-test",
        worker_generation=1,
        agent_config_id=CONFIG_ID,
        config_checksum="sha256:" + "1" * 64,
        prompt_id="phase0_general_voice_assistant_v1",
        prompt_version=1,
        prompt_checksum=PHASE0_PROMPT_CHECKSUM,
        system_instruction=PHASE0_SYSTEM_INSTRUCTION,
        max_output_tokens=250,
        provider="openai",
        model="gpt-6-luna",
        retry=no_wait,
    )
    speech_setup = SpeechSetup(
        session_id=SESSION_ID,
        worker_generation=1,
        provider="sarvam",
        model="bulbul:v3",
        voice_id="priya",
        retry=no_wait,
        ack_grace_ms=ack_grace_ms,
    )
    gate = SpeakingConversationGate(
        setup,
        engine=OpenAiConversationAdapter(llm_connector, clock=clock),
        speech=SpeechConfig(tts=adapter, transport=transport, voice=VOICE, setup=speech_setup),
        evidence=evidence,
        publisher=RealtimePublisher(
            transport, session_id=SESSION_ID, correlation_id="wp9-test", clock=clock, ids=ids
        ),
        clock=clock,
        ids=ids,
        jitter=lambda: 0.0,
    )
    if isinstance(transport, AckingTransport):
        transport.gate = gate
    return Rig(gate, llm_connector, sarvam, adapter, transport, turns, operations, events, costs)


async def _until(predicate: Any, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():  # noqa: ASYNC110 - polls a fake's recorded state
            await asyncio.sleep(0.005)


async def test_accepted_turn_is_spoken_segment_by_segment_with_evidence_and_cost() -> None:
    rig = build([reply("Namaste Arun! ", "Aap kaise hain?")], [speak(3), speak(2.5)])
    turn = await rig.accept("Namaste, mera naam Arun hai.")

    assert await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    # Real audio flowed through the transport, one playback segment per piece.
    segments = rig.transport.segments_published()
    assert len(segments) == 2
    assert len(rig.transport.published) == 6
    assert all(f.identity.worker_generation == 1 for f in rig.transport.published)
    assert all(f.frame.sample_rate_hz == 24_000 for f in rig.transport.published)
    assert [i.segment_id for i in rig.transport.finished] == segments
    assert rig.playback() == ["started", "completed", "started", "completed"]
    playback = rig.on("va.playback.v1")
    assert playback[0]["payload"]["segment_id"] == segments[0]
    assert playback[0]["turn_id"] == turn.turn_id
    assert rig.states() == ["thinking", "speaking", "listening"]
    responses = rig.on("va.response.v1")
    assert [r["payload"]["text"] for r in responses] == [
        "Namaste Arun!",
        "Namaste Arun! Aap kaise hain?",
        "Namaste Arun! Aap kaise hain?",
    ]
    assert responses[-1]["payload"]["response_completion_status"] == "completed"
    stored = await rig.turn(turn.turn_id)
    assert stored.status is TurnStatus.COMPLETED
    assert stored.generated_text == "Namaste Arun! Aap kaise hain?"
    assert stored.synthesized_text == "Namaste Arun! Aap kaise hain?"
    assert stored.spoken_text == "Namaste Arun! Aap kaise hain?"
    assert stored.spoken_text_accuracy is SpokenTextAccuracy.ESTIMATED  # no browser acks
    assert rig.sarvam.texts == ["Namaste Arun!", "Aap kaise hain?"]
    assert rig.sarvam.opens == 1  # the clean connection was reused
    operations = await rig.tts_operations()
    assert [o.status for o in operations] == [OperationStatus.SUCCEEDED] * 2
    first = operations[0]
    assert first.operation_type == "synthesize_stream"
    assert (first.provider, first.model) == ("sarvam", "bulbul:v3")
    assert first.time_to_first_result_ms is not None
    assert first.first_result_at is not None
    assert first.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == len("Namaste Arun!")
    assert first.result_summary is not None
    assert first.result_summary["generated_audio_ms"] == 60
    assert first.result_summary["normalization_version"] == "phase0_tts_text_v1"
    tts_lines = [
        line
        for run in rig.costs.runs
        for line in run
        if line.provider_identity.provider == "sarvam"
        and line.quantity.native_unit == "synthesized_characters"
    ]
    assert sum((line.amounts.gross_cost for line in tts_lines), Decimal(0)) == Decimal(
        len("Namaste Arun!") + len("Aap kaise hain?")
    ) * Decimal("3.00") / Decimal(1000)
    assert {line.currency_conversion.original_currency.value for line in tts_lines} == {"INR"}
    types = rig.events.types()
    for expected in (
        EventType.TTS_SEGMENT_STARTED,
        EventType.TTS_FIRST_AUDIO,
        EventType.TTS_SEGMENT_COMPLETED,
        EventType.TTS_USAGE,
        EventType.PLAYBACK_STARTED,
        EventType.PLAYBACK_COMPLETED,
        EventType.TURN_COMPLETED,
    ):
        assert expected in types
    assert rig.gate.history[-1].text == "Namaste Arun! Aap kaise hain?"


async def test_generated_normalized_synthesized_and_spoken_evidence_stay_distinct() -> None:
    rig = build([reply("**Fees** Rs. 125000 hai, e.g. abhi!!!")], [speak(2)])
    turn = await rig.accept("Fees kitni hai?")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    stored = await rig.turn(turn.turn_id)
    assert stored.generated_text == "**Fees** Rs. 125000 hai, e.g. abhi!!!"
    assert stored.synthesized_text == "Fees ₹1,25,000 hai, for example abhi!"
    assert stored.spoken_text == stored.synthesized_text
    # The browser and history get the delivered (segmenter) text, not the TTS form.
    assert rig.finals()[0]["payload"]["text"] == "Fees Rs. 125000 hai, e.g. abhi!!!"
    assert rig.gate.history[-1].text == "Fees Rs. 125000 hai, e.g. abhi!!!"
    [operation] = await rig.tts_operations()
    assert operation.result_summary is not None
    assert operation.result_summary["original_characters"] == len(
        "Fees Rs. 125000 hai, e.g. abhi!!!"
    )
    assert operation.result_summary["normalized_characters"] == len(stored.synthesized_text)


async def test_browser_acknowledgements_confirm_spoken_text() -> None:
    transport = AckingTransport()
    rig = build([reply("Theek hai.")], [speak(2)], transport=transport, ack_grace_ms=500)
    turn = await rig.accept("Ok?")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    stored = await rig.turn(turn.turn_id)
    assert stored.spoken_text_accuracy is SpokenTextAccuracy.CONFIRMED


async def test_unknown_acknowledgements_are_ignored_evidence() -> None:
    rig = build([])
    stray = PlaybackAckIdentity(
        worker_generation=1, cancellation_generation=9, segment_id=SESSION_ID
    )

    await rig.gate.on_playback_ack(PlaybackAck(ack=PlaybackAckKind.COMPLETED, identity=stray))

    assert rig.gate.ignored_acks == 1


async def test_newer_turn_interrupts_speech_clears_playback_and_never_plays_old_audio() -> None:
    rig = build(
        [reply("Pehla jawab bahut lamba hai."), reply("Doosra jawab.")],
        [[audio(2), PAUSE], speak(2)],
    )
    first = await rig.accept("Pehla sawaal")
    await rig.gate.authorize(first)
    await _until(lambda: len(rig.transport.published) >= 2)
    old_generation = rig.transport.published[0].identity.cancellation_generation

    second = await rig.accept("Ruko, doosra sawaal")
    await rig.gate.authorize(second)
    await rig.gate.wait_idle()

    assert rig.transport.clears >= 1
    assert "cancelled" in rig.playback()
    assert "interrupted" in rig.states()
    old_frames = [
        f for f in rig.transport.published if f.identity.cancellation_generation == old_generation
    ]
    assert len(old_frames) == 2  # nothing of the old turn after the interruption
    new_frames = [
        f for f in rig.transport.published if f.identity.cancellation_generation > old_generation
    ]
    assert len(new_frames) == 2
    old = await rig.turn(first.turn_id)
    assert old.status is TurnStatus.INTERRUPTED
    assert old.interruption.reason is InterruptionReason.USER_BARGE_IN
    assert old.interruption.phase is InterruptionPhase.SPEAKING
    assert old.spoken_text == "Pehla jawab bahut lamba hai."
    assert old.spoken_text_accuracy is SpokenTextAccuracy.ESTIMATED
    statuses = [o.status for o in await rig.tts_operations()]
    assert OperationStatus.CANCELLED in statuses
    assert (await rig.turn(second.turn_id)).status is TurnStatus.COMPLETED
    assert rig.sarvam.streams[0].closed  # the interrupted connection was discarded


async def test_uncooperative_provider_audio_after_cancel_never_reaches_playback() -> None:
    mock = MockTtsAdapter(
        frames_per_segment=6, pause_when=lambda r: r.text.startswith("Ek"), honor_cancel=False
    )
    rig = build([reply("Ek lambi baat."), reply("Nayi baat.")], tts=mock)
    first = await rig.accept("Pehle")
    await rig.gate.authorize(first)
    await _until(lambda: len(rig.transport.published) >= 1)

    second = await rig.accept("Phir")
    await rig.gate.authorize(second)
    await rig.gate.wait_idle()

    generations = [f.identity.cancellation_generation for f in rig.transport.published]
    old = generations[0]
    assert generations.count(old) == 1  # the five late frames were dropped, not played
    assert all(g > old for g in generations[1:])
    assert len(generations) == 1 + 6
    assert (await rig.turn(second.turn_id)).status is TurnStatus.COMPLETED


async def test_transient_failure_before_audio_retries_as_a_new_attempt() -> None:
    rig = build([reply("Haan ji.")], [[error(503)], speak(2)])
    turn = await rig.accept("Suno")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    failed_op, retried = await rig.tts_operations()
    assert failed_op.status is OperationStatus.FAILED
    assert retried.status is OperationStatus.SUCCEEDED
    assert retried.attempt_number == 2
    assert retried.logical_request_id == failed_op.logical_request_id
    assert retried.previous_attempt_operation_id == failed_op.operation_id
    assert (await rig.turn(turn.turn_id)).status is TurnStatus.COMPLETED
    assert rig.sarvam.texts == ["Haan ji.", "Haan ji."]


async def test_non_retryable_failure_with_nothing_heard_fails_the_turn_safely() -> None:
    rig = build([reply("Kuch bhi.")], [[error(401)]])
    turn = await rig.accept("Hello")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    stored = await rig.turn(turn.turn_id)
    assert stored.status is TurnStatus.FAILED
    assert stored.spoken_text == ""
    errors = rig.on("va.error.v1")
    assert errors[0]["payload"]["code"] == "speech_unavailable"
    assert "fallback_template_id" not in rig.finals()[0]["payload"]  # TTS is down: not spoken
    assert rig.transport.published == []
    assert len(await rig.tts_operations()) == 1


async def test_failure_after_partial_speech_fails_without_replaying_audio() -> None:
    rig = build([reply("Pehli line. ", "Doosri line.")], [speak(2), [audio(1), error(503)]])
    turn = await rig.accept("Batao")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    stored = await rig.turn(turn.turn_id)
    assert stored.status is TurnStatus.FAILED
    assert stored.spoken_text.startswith("Pehli line.")
    assert rig.sarvam.texts == ["Pehli line.", "Doosri line."]  # no retry after audio


async def test_llm_failure_speaks_the_fallback_phrase() -> None:
    rig = build([[failed("invalid_prompt")]], [speak(3)])
    turn = await rig.accept("Kya haal hai?")

    await rig.gate.authorize(turn)
    await rig.gate.wait_idle()

    [final] = rig.finals()
    assert final["payload"]["fallback_template_id"] == RESPONSE_FAILED.template_id
    assert final["payload"]["response_completion_status"] == "failed"
    stored = await rig.turn(turn.turn_id)
    assert stored.fallback_used
    assert stored.spoken_text
    assert rig.transport.published


async def test_interrupt_cancels_active_speech_and_close_closes_tts() -> None:
    rig = build([reply("Lamba jawab.")], [[audio(2), PAUSE]])
    turn = await rig.accept("Bolo")
    await rig.gate.authorize(turn)
    await _until(lambda: len(rig.transport.published) >= 2)

    await rig.gate.interrupt()
    await rig.gate.wait_idle()
    await rig.gate.close()

    stored = await rig.turn(turn.turn_id)
    assert stored.status is TurnStatus.INTERRUPTED
    assert stored.response_completion_status is ResponseCompletionStatus.INTERRUPTED
    assert rig.sarvam.closed


async def test_start_opens_the_tts_session_once() -> None:
    rig = build([])

    await rig.gate.start()
    await rig.gate.start()
    await rig.gate.close()

    assert rig.sarvam.opens == 0  # prewarm is off in this rig; open_session ran once
    assert rig.sarvam.closed
