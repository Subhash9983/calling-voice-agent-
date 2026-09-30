"""STT check session: local VAD authority, 700 ms endpoint, durable-before-authorized (WP7).

Runs the real Turn Manager, the energy mock detector (Silero stand-in), and
the real Deepgram adapter over the scripted fake connector. Offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from tests.support.fake_deepgram import FakeDeepgramConnector, results
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport

from voice_agent.agent_worker.realtime_publisher import RealtimePublisher, sanitize_transcript
from voice_agent.agent_worker.stt_check import NoGenerationGate, SttCheck, SttCheckSetup
from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.enums import InputDisposition, OperationStatus, TurnStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.stt import SttFailed, SttStreamConfig, SttTurnFinalized
from voice_agent.contracts.transport import ClientReady
from voice_agent.contracts.usage import UsageUnit
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord
from voice_agent.speech_activity.mock import MockSpeechActivityDetector
from voice_agent.stt_adapters.deepgram.adapter import DeepgramSttAdapter, DeepgramTiming

pytestmark = pytest.mark.asyncio
SPEECH, SILENCE = 0.8, 0.0
CONFIG_ID = "00000000-0000-4000-8000-00000000c7a1"


@dataclass
class EventLog:
    records: list[EventRecord] = field(default_factory=list)

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()

    def of(self, event_type: EventType) -> list[dict[str, Any]]:
        return [
            dict(r.envelope.payload) for r in self.records if r.envelope.event_type is event_type
        ]


@dataclass
class CostRuns:
    runs: list[Sequence[CostEntryRecord]] = field(default_factory=list)

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        self.runs.append(entries)


class RecordingTurns(InMemoryTurnRepository):
    """Records save order and can refuse the accepted-transcript save."""

    def __init__(self, order: list[str], *, fail_accepted: bool = False) -> None:
        super().__init__()
        self._order = order
        self._fail_accepted = fail_accepted

    async def save(self, turn: ConversationTurn) -> None:
        if self._fail_accepted and turn.status is TurnStatus.TRANSCRIPT_FINAL:
            raise RuntimeError("store unavailable")
        self._order.append(f"save:{turn.status.value}")
        await super().save(turn)


class RecordingGate(NoGenerationGate):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def authorize(self, turn: ConversationTurn) -> None:
        self._order.append("authorize")
        await super().authorize(turn)


@dataclass
class Rig:
    transport: FakeSessionTransport
    connector: FakeDeepgramConnector
    check: SttCheck
    turns: RecordingTurns
    operations: InMemoryOperationRepository
    events: EventLog
    costs: CostRuns
    order: list[str]
    task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.check.run())
        await asyncio.wait_for(self.transport.wait_for_subscribers(2), timeout=1)

    async def stop(self) -> None:
        assert self.task is not None
        await asyncio.sleep(0.05)
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task

    def transcripts(self) -> list[dict[str, Any]]:
        return [s.body for s in self.transport.on("va.transcript.v1")]

    def states(self) -> list[str]:
        return [s.body["payload"]["state"] for s in self.transport.on("va.state.v1")]


def build(
    transcripts: Sequence[str] = ("मेरा नाम Arun है।",),
    *,
    fail_accepted: bool = False,
    connector: FakeDeepgramConnector | None = None,
) -> Rig:
    transport = FakeSessionTransport()
    connector = connector or FakeDeepgramConnector(list(transcripts))
    clock, ids = SystemClock(), UuidIdGenerator()
    order: list[str] = []
    turns = RecordingTurns(order, fail_accepted=fail_accepted)
    operations = InMemoryOperationRepository()
    events, costs = EventLog(), CostRuns()
    policy = TurnHandlingPolicy()
    evidence = SttEvidence(
        EvidenceContext(
            session_id=SESSION_ID,
            correlation_id="wp7-test",
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
    stt = DeepgramSttAdapter(
        connector,
        session_id=SESSION_ID,
        worker_generation=1,
        clock=clock,
        ids=ids,
        timing=DeepgramTiming(finalize_timeout_ms=300, close_timeout_s=0.5),
    )
    check = SttCheck(
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
        publisher=RealtimePublisher(
            transport, session_id=SESSION_ID, correlation_id="wp7-test", clock=clock, ids=ids
        ),
        clock=clock,
        ids=ids,
        gate=RecordingGate(order),
    )
    return Rig(transport, connector, check, turns, operations, events, costs, order)


async def utterance(rig: Rig, first: int = 0, speech_frames: int = 20) -> int:
    """Speech then 40 frames (800 ms) of silence: past the 700 ms endpoint deadline."""
    after_speech = await rig.transport.speak(first, speech_frames, SPEECH)
    end = await rig.transport.speak(after_speech, 40, SILENCE)
    await asyncio.sleep(0.05)
    return end


async def test_local_vad_endpoint_commits_700_ms_after_last_speech_not_1250() -> None:
    rig = build()
    await rig.start()

    await utterance(rig)
    await rig.stop()

    ended = rig.events.of(EventType.USER_SPEECH_ENDED)
    assert len(ended) == 1
    assert ended[0]["last_speech_at_ms"] == 400
    assert ended[0]["committed_at_ms"] == 1100
    assert ended[0]["endpoint_delay_ms"] == 700
    assert rig.connector.current.finalizes == 1


async def test_accepted_transcript_is_durable_before_publication_and_authorization() -> None:
    rig = build()
    await rig.start()

    await utterance(rig)
    await rig.stop()

    finals = [t for t in rig.transcripts() if t["payload"]["is_final"]]
    assert len(finals) == 1
    assert finals[0]["event_type"] == "stt.final"
    assert finals[0]["payload"] == {"text": "मेरा नाम Arun है।", "is_final": True}
    assert finals[0]["turn_id"] is not None
    assert finals[0]["sequence_number"] >= 1
    assert rig.order.index("save:transcript_final") < rig.order.index("authorize")
    assert rig.check.gate.offered == [finals[0]["turn_id"]]  # type: ignore[attr-defined]
    assert rig.states()[:3] == ["listening", "transcribing", "listening"]
    turn = await rig.turns.get(finals[0]["turn_id"])
    assert turn is not None
    assert turn.status is TurnStatus.ABANDONED
    assert turn.final_transcript == "मेरा नाम Arun है।"


async def test_transcript_sequence_numbers_increase_and_partials_carry_the_turn() -> None:
    rig = build(["first", "second"])
    await rig.start()

    next_frame = await rig.transport.speak(0, 20, SPEECH)
    rig.connector.current.push(results("fir", 0.0, 0.3, is_final=False))
    await asyncio.sleep(0.02)
    next_frame = await rig.transport.speak(next_frame, 40, SILENCE)
    await asyncio.sleep(0.05)
    await utterance(rig, first=next_frame)
    await rig.stop()

    messages = rig.transcripts()
    sequences = [m["sequence_number"] for m in messages]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    partial = next(m for m in messages if not m["payload"]["is_final"])
    assert partial["event_type"] == "stt.partial"
    assert partial["turn_id"] is not None
    assert [m["payload"]["text"] for m in messages if m["payload"]["is_final"]] == [
        "first",
        "second",
    ]
    assert len({m["turn_id"] for m in messages if m["payload"]["is_final"]}) == 2


async def test_provider_endpoint_signals_cannot_close_the_turn() -> None:
    rig = build()
    await rig.start()

    next_frame = await rig.transport.speak(0, 20, SPEECH)
    connection = rig.connector.current
    connection.push(results("मेरा नाम", 0.0, 0.4, is_final=True, speech_final=True))
    connection.push({"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": 0.4})
    connection.push({"type": "SpeechStarted", "channel": [0], "timestamp": 0.0})
    await asyncio.sleep(0.05)

    assert connection.finalizes == 0
    assert rig.events.of(EventType.USER_SPEECH_ENDED) == []
    assert not [t for t in rig.transcripts() if t["payload"]["is_final"]]
    await rig.transport.speak(next_frame, 40, SILENCE)
    await rig.stop()
    assert connection.finalizes == 1


async def test_empty_transcript_discards_the_turn_and_authorizes_nothing() -> None:
    rig = build([""])
    await rig.start()

    await utterance(rig)
    await rig.stop()

    assert not [t for t in rig.transcripts() if t["payload"]["is_final"]]
    assert rig.check.gate.offered == []  # type: ignore[attr-defined]
    discarded = rig.events.of(EventType.TURN_DISCARDED)
    assert discarded == [{"reason": "empty_transcript"}]
    stored = await rig.turns.list_for_session(SESSION_ID)
    assert stored[0].status is TurnStatus.DISCARDED
    assert stored[0].input_disposition is InputDisposition.EMPTY


async def test_transcript_that_is_not_durable_is_never_shown_final_or_authorized() -> None:
    rig = build(fail_accepted=True)
    await rig.start()

    await utterance(rig)
    await rig.stop()

    assert not [t for t in rig.transcripts() if t["payload"]["is_final"]]
    assert "authorize" not in rig.order
    errors = [s.body["payload"]["code"] for s in rig.transport.on("va.error.v1")]
    assert errors == ["transcript_not_saved"]


async def test_stale_or_foreign_transcripts_are_discarded_late() -> None:
    rig = build()
    await rig.start()
    stale = GenerationStamp(
        session_id=SESSION_ID,
        turn_id="00000000-0000-4000-8000-0000000000ff",
        worker_generation=1,
        cancellation_generation=0,
    )
    old_worker = stale.model_copy(update={"worker_generation": 2})

    for stamp in (stale, old_worker):
        await rig.check._on_stt(
            SttTurnFinalized(
                stamp=stamp, transcript_id="00000000-0000-4000-8000-0000000000fe", text="late"
            )
        )
    await rig.stop()

    assert rig.check.counters["late_transcripts"] == 2
    assert rig.check.gate.offered == []  # type: ignore[attr-defined]


async def test_stream_evidence_is_persisted_with_usage_timing_and_decimal_cost() -> None:
    rig = build()
    await rig.start()

    await utterance(rig)
    await rig.stop()

    operations = await rig.operations.list_for_session(SESSION_ID)
    assert len(operations) == 1
    stream = operations[0]
    assert stream.status is OperationStatus.SUCCEEDED
    assert stream.operation_type == "stt_stream"
    assert stream.provider == "deepgram"
    assert stream.model == "nova-3"
    seconds = stream.usage.quantity_of(UsageUnit.TRANSCRIBED_AUDIO_SECONDS)
    assert seconds == Decimal("1.2")  # 60 frames x 20 ms, reported by Metadata
    assert stream.started_at is not None
    assert stream.result_summary is not None
    assert stream.result_summary["sent_audio_ms"] == 1200
    scopes = [run[0].scope for run in rig.costs.runs]
    assert scopes == [CostScope.OPERATION, CostScope.SESSION]
    line = rig.costs.runs[-1][0]
    assert line.rate.rate_card_version == "phase0_rate_card_2026_09_26_v1"
    expected = (Decimal("1.2") * Decimal("0.0092") / 60).quantize(Decimal("1e-12"))
    assert line.currency_conversion.converted_net_cost == expected
    assert line.quantity.native_unit == "transcribed_audio_seconds"


async def test_turn_failure_from_stt_fails_only_that_turn() -> None:
    rig = build()
    await rig.start()
    rig.connector.answer_finalize = False
    await rig.transport.speak(0, 20, SPEECH)
    await rig.transport.speak(20, 40, SILENCE)
    await asyncio.sleep(0.01)
    waiting = next(iter(rig.check._awaiting.values()), None)
    turn_id = waiting.turn.turn_id if waiting else None

    if turn_id is not None:
        failure = NormalizedFailure(
            component=ErrorComponent.STT,
            error_type=ErrorType.CONNECTION_LOST,
            safe_message="lost",
            retryable=True,
            session_id=SESSION_ID,
            turn_id=turn_id,
            occurred_at=SystemClock().utc_now(),
        )
        stamp = GenerationStamp(
            session_id=SESSION_ID, turn_id=turn_id, worker_generation=1, cancellation_generation=0
        )
        await rig.check._on_stt(SttFailed(stamp=stamp, failure=failure))
    await rig.stop()

    stored = await rig.turns.list_for_session(SESSION_ID)
    assert turn_id is not None
    assert stored[0].status is TurnStatus.FAILED
    assert rig.events.of(EventType.TURN_FAILED) == [{"error_type": "connection_lost"}]
    assert [s.body["payload"]["code"] for s in rig.transport.on("va.error.v1")] == [
        "stt_turn_failed"
    ]


async def test_stt_start_failure_reports_error_and_never_finalizes() -> None:
    from voice_agent.stt_adapters.deepgram.connection import (
        DeepgramErrorKind,
        DeepgramTransportError,
    )

    connector = FakeDeepgramConnector(
        connect_errors=[DeepgramTransportError(DeepgramErrorKind.AUTHENTICATION, 401)]
    )
    rig = build(connector=connector)
    rig.task = asyncio.create_task(rig.check.run())
    await asyncio.wait_for(rig.transport.wait_for_subscribers(1), timeout=1)

    await utterance(rig)
    await rig.stop()

    assert rig.states()[0] == "error"
    assert [s.body["payload"]["code"] for s in rig.transport.on("va.error.v1")] == [
        "stt_unavailable"
    ]
    assert rig.events.of(EventType.TURN_DISCARDED) == [{"reason": "stt_unavailable"}]
    operations = await rig.operations.list_for_session(SESSION_ID)
    assert [o.status for o in operations] == [OperationStatus.FAILED]


async def test_endpoint_timer_commits_when_microphone_frames_stop() -> None:
    rig = build()
    await rig.start()

    next_frame = await rig.transport.speak(0, 20, SPEECH)
    await rig.transport.speak(next_frame, 28, SILENCE)  # VAD stop at 960 ms, then no frames
    await asyncio.sleep(0.4)
    await rig.stop()

    ended = rig.events.of(EventType.USER_SPEECH_ENDED)
    assert ended
    assert ended[0]["endpoint_delay_ms"] == 700
    assert rig.check.counters["endpoint_timer_commits"] == 1


async def test_client_ready_replays_the_current_state() -> None:
    rig = build()
    await rig.start()

    rig.transport.client(ClientReady())
    await asyncio.sleep(0.02)
    await rig.stop()

    assert rig.states()[:2] == ["listening", "listening"]


async def test_published_transcripts_are_sanitized_and_bounded() -> None:
    assert sanitize_transcript("  नमस्ते\x00 \n दुनिया  ") == "नमस्ते दुनिया"
    assert len(sanitize_transcript("x" * 5000)) == 2000
