"""Offline rig for the WP10 conversation orchestrator (no network, no key).

Real orchestrator + Turn Manager + energy mock VAD (Silero stand-in, 0.5 /
0.7 thresholds) + Deepgram adapter (scripted fake seam) + OpenAI adapter
(scripted fake Responses stream) + mock or Sarvam TTS + in-memory
repositories, over a session transport fake that reports agent audio as
active from the first published frame until playout completes or the queue
is cleared (the LiveKit ``agent_audio_active`` probe).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_openai import FakeResponsesConnector
from tests.support.fake_openai import Step as LlmStep
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport, mic_frame

from voice_agent.agent_worker.conversation_gate import OrchestratedGate
from voice_agent.agent_worker.conversation_orchestrator import ConversationOrchestrator
from voice_agent.agent_worker.conversation_policy import ConversationTimeouts
from voice_agent.agent_worker.llm_gate import ConversationSetup
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.stt_check import SttCheckSetup
from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.agent_worker.tts_gate import SpeechConfig
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.contracts.policies import RetryPolicy, TurnHandlingPolicy
from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.contracts.transport import PlaybackFrame, RealtimeTopic
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.conversation_adapters.openai.adapter import OpenAiConversationAdapter
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord
from voice_agent.ports.tts import TTSPort
from voice_agent.provider_registry.phase0_prompt import (
    PHASE0_PROMPT_CHECKSUM,
    PHASE0_SYSTEM_INSTRUCTION,
)
from voice_agent.speech_activity.mock import MockSpeechActivityDetector
from voice_agent.stt_adapters.deepgram.adapter import DeepgramSttAdapter, DeepgramTiming
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter

SPEECH, NOISE, SILENCE = 0.8, 0.6, 0.0
CONFIG_ID = "00000000-0000-4000-8000-00000000c9a1"
VOICE = TtsVoiceConfig(provider="mock_tts", model="mock-tts-v1", voice_id="priya")
FAR = 10**9


class PlayingTransport(FakeSessionTransport):
    """Agent audio counts as audible from the first frame until playout or a clear.

    With ``auto_playout`` off, each segment's playout blocks until
    :meth:`release_playout` (or a clear), so a test can speak "over" audio.
    """

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order
        self.auto_playout = True
        self.hold_playout = asyncio.Event()

    async def publish_audio(self, frame: PlaybackFrame) -> None:
        self.agent_audio_active = True
        await super().publish_audio(frame)

    async def clear_playback(self) -> None:
        self.order.append("transport.clear_queue")
        self.agent_audio_active = False
        await super().clear_playback()

    async def wait_for_playout(self) -> None:
        self.playouts += 1
        self.playout_reached.set()
        if not self.auto_playout:
            await self.hold_playout.wait()
            self.hold_playout.clear()
        self.agent_audio_active = False

    def release_playout(self) -> None:
        self.hold_playout.set()

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        if topic is RealtimeTopic.PLAYBACK and envelope.payload.get("state") == "cancelled":
            self.order.append("browser.cancelled")
        if topic is RealtimeTopic.STATE and envelope.payload.get("state") == "interrupted":
            self.order.append("browser.interrupted")
        await super().send_event(topic, envelope, reliable=reliable)


class RecordingEngine(OpenAiConversationAdapter):
    order: list[str]

    async def cancel(self, operation_id: str) -> None:
        self.order.append("llm.cancel")
        await super().cancel(operation_id)


@dataclass
class EventLog:
    records: list[EventRecord] = field(default_factory=list)
    fail: bool = False

    async def append(self, record: EventRecord) -> None:
        if self.fail:
            raise RuntimeError("event store unavailable")
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()

    def of(self, event_type: EventType) -> list[EventRecord]:
        return [r for r in self.records if r.envelope.event_type is event_type]


@dataclass
class CostRuns:
    runs: list[Sequence[CostEntryRecord]] = field(default_factory=list)

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        self.runs.append(entries)


@dataclass
class Rig:
    orchestrator: ConversationOrchestrator
    gate: OrchestratedGate
    transport: PlayingTransport
    deepgram: FakeDeepgramConnector
    llm: FakeResponsesConnector
    tts: TTSPort
    turns: InMemoryTurnRepository
    operations: InMemoryOperationRepository
    events: EventLog
    order: list[str]
    ended: list[DisconnectReason]
    frame: int = 0
    task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.orchestrator.run())
        await asyncio.wait_for(self.transport.wait_for_subscribers(2), timeout=1)

    async def stop(self) -> None:
        assert self.task is not None
        if not self.task.done():
            self.task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(self.task), timeout=10)

    async def feed(self, count: int, level: float) -> None:
        for _ in range(count):
            self.transport.feed(mic_frame(self.frame, level))
            self.frame += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)

    async def utterance(self, frames: int = 20) -> None:
        """Speech, then 40 frames (800 ms) of silence: past the 700 ms endpoint."""
        await self.feed(frames, SPEECH)
        await self.feed(40, SILENCE)

    async def until(self, predicate: Callable[[], bool], timeout_s: float = 3.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not predicate():  # noqa: ASYNC110 - polls fakes' recorded state
                await asyncio.sleep(0.005)

    async def idle(self) -> None:
        """Wait until no transcript is pending and no response (or phrase) is active."""
        orchestrator = self.orchestrator
        await self.until(lambda: not self.gate.responding and not orchestrator._awaiting)
        await self.gate.wait_idle()

    async def all_turns(self) -> list[ConversationTurn]:
        return list(await self.turns.list_for_session(SESSION_ID))

    def states(self) -> list[str]:
        return [s.body["payload"]["state"] for s in self.transport.on("va.state.v1")]

    def responses(self) -> list[dict[str, Any]]:
        return [s.body for s in self.transport.on("va.response.v1")]

    def finals(self) -> list[dict[str, Any]]:
        return [b for b in self.responses() if b["payload"]["is_final"]]

    def playback(self) -> list[str]:
        return [s.body["payload"]["state"] for s in self.transport.on("va.playback.v1")]


def build(
    llm: Sequence[Sequence[LlmStep]] = (),
    transcripts: Sequence[str] = (),
    *,
    tts: TTSPort | None = None,
    greeting: bool = False,
    timeouts: ConversationTimeouts | None = None,
    deepgram: FakeDeepgramConnector | None = None,
    finalize_timeout_ms: int = 300,
    clock: SystemClock | None = None,
    deadline_at: datetime | None = None,
) -> Rig:
    order: list[str] = []
    ended: list[DisconnectReason] = []
    transport = PlayingTransport(order)
    clock = clock or SystemClock()
    ids = UuidIdGenerator()
    connector = deepgram or FakeDeepgramConnector(list(transcripts))
    turns, operations = InMemoryTurnRepository(), InMemoryOperationRepository()
    events = EventLog()
    evidence = SttEvidence(
        EvidenceContext(
            session_id=SESSION_ID,
            correlation_id="wp10-test",
            agent_config_id=CONFIG_ID,
            environment=AgentConfigEnvironment.DEVELOPMENT,
            worker_generation=1,
        ),
        turns=turns,
        operations=operations,
        events=events,
        costs=CostRuns(),
        rate_card=phase0_rate_card(),
        clock=clock,
        ids=ids,
    )
    publisher = RealtimePublisher(
        transport, session_id=SESSION_ID, correlation_id="wp10-test", clock=clock, ids=ids
    )
    no_wait = RetryPolicy(initial_backoff_ms=0, maximum_backoff_ms=0)
    llm_connector = FakeResponsesConnector(llm)
    engine = RecordingEngine(llm_connector, clock=clock)
    engine.order = order
    adapter = tts or MockTtsAdapter(frames_per_segment=3, record=order.append)
    gate = OrchestratedGate(
        ConversationSetup(
            session_id=SESSION_ID,
            correlation_id="wp10-test",
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
        ),
        engine=engine,
        speech=SpeechConfig(
            tts=adapter,
            transport=transport,
            voice=VOICE,
            setup=SpeechSetup(
                session_id=SESSION_ID,
                worker_generation=1,
                provider="mock_tts",
                model="mock-tts-v1",
                voice_id="priya",
                retry=no_wait,
                ack_grace_ms=0,
            ),
        ),
        evidence=evidence,
        publisher=publisher,
        clock=clock,
        ids=ids,
        jitter=lambda: 0.0,
    )
    policy = TurnHandlingPolicy()
    stt = DeepgramSttAdapter(
        connector,
        session_id=SESSION_ID,
        worker_generation=1,
        clock=clock,
        ids=ids,
        timing=DeepgramTiming(finalize_timeout_ms=finalize_timeout_ms, close_timeout_s=0.5),
    )
    orchestrator = ConversationOrchestrator(
        transport,
        SttCheckSetup(
            session_id=SESSION_ID,
            worker_generation=1,
            policy=policy,
            stt_config=SttStreamConfig(),
        ),
        stt=stt,
        detector=MockSpeechActivityDetector(policy),
        evidence=evidence,
        publisher=publisher,
        clock=clock,
        ids=ids,
        gate=gate,
        timeouts=timeouts
        or ConversationTimeouts(
            maximum_silence_ms=FAR,
            maximum_user_turn_ms=FAR,
            idle_session_ms=FAR,
            maximum_duration_deadline_at=deadline_at,
            tick_s=0.01,
        ),
        request_end=ended.append,
        greeting_enabled=greeting,
    )
    return Rig(
        orchestrator,
        gate,
        transport,
        connector,
        llm_connector,
        adapter,
        turns,
        operations,
        events,
        order,
        ended,
    )
