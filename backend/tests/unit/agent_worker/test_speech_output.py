"""Speech output over the real LiveKit session transport (WP6 clear_queue pattern; WP9).

The fake room gateway's sink stands in for ``rtc.AudioSource``: what it
captured is exactly what could become audible. A provider that ignores
cancellation keeps producing frames; none of them may reach the sink once the
fence advanced, and ``clear_queue()`` must run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from tests.support.fake_livekit import Decimate, FakeGateway

from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_output import SpeechDeps, SpeechTurn
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.tts import MAX_TTS_SEGMENT_CHARS, TtsVoiceConfig
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.events_and_latency.clock import ManualClock, SystemClock, UuidIdGenerator
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.orchestration.response_generation import DeliveredSegment
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord
from voice_agent.ports.tts import TTSPort
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter

pytestmark = pytest.mark.asyncio
SESSION_ID = "00000000-0000-4000-8000-000000000001"
TURN_ID = "00000000-0000-4000-8000-000000000003"
BROWSER, AGENT = "va-user-1", "va-agent-1"
HI = TtsLanguageCode.HI_IN
VOICE = TtsVoiceConfig(provider="mock_tts", model="mock-tts-v1", voice_id="mock")


@dataclass
class Sink:
    records: list[EventRecord] = field(default_factory=list)

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        return None


@dataclass
class Rig:
    speech: SpeechTurn
    fence: GenerationFence
    gateway: FakeGateway
    tts: MockTtsAdapter

    def playback(self) -> list[str]:
        return [
            s.body["payload"]["state"] for s in self.gateway.sent if s.topic == "va.playback.v1"
        ]


async def build(tts: MockTtsAdapter, *, queued: int = 5) -> Rig:
    gateway = FakeGateway()
    gateway.present.add(BROWSER)
    transport = LiveKitSessionTransport(
        session_id=SESSION_ID,
        browser_identity=BROWSER,
        agent_identity=AGENT,
        worker_generation=1,
        gateway=gateway,
        clock=ManualClock(),
        resampler_factory=Decimate,
    )
    await transport.connect()
    clock, ids, sink = SystemClock(), UuidIdGenerator(), Sink()
    fence = GenerationFence(session_id=SESSION_ID, worker_generation=1)
    fence.activate_turn(TURN_ID)
    evidence = SttEvidence(
        EvidenceContext(SESSION_ID, "wp9", SESSION_ID, AgentConfigEnvironment.DEVELOPMENT, 1),
        turns=InMemoryTurnRepository(),
        operations=InMemoryOperationRepository(),
        events=sink,
        costs=sink,
        rate_card=phase0_rate_card(),
        clock=clock,
        ids=ids,
    )
    await tts.open_session(VOICE)
    adapter: TTSPort = tts
    deps = SpeechDeps(
        setup=SpeechSetup(
            session_id=SESSION_ID,
            worker_generation=1,
            provider="mock_tts",
            model="mock-tts-v1",
            voice_id="mock",
            retry=RetryPolicy(initial_backoff_ms=0, maximum_backoff_ms=0),
            max_queued_segments=queued,
            ack_grace_ms=0,
        ),
        tts=adapter,
        transport=transport,
        fence=fence,
        evidence=evidence,
        publisher=RealtimePublisher(
            transport, session_id=SESSION_ID, correlation_id="wp9", clock=clock, ids=ids
        ),
        clock=clock,
        ids=ids,
        jitter=lambda: 0.0,
    )
    speech = SpeechTurn(deps, turn_stamp=fence.stamp(turn_id=TURN_ID))
    return Rig(speech, fence, gateway, tts)


async def _until(predicate: object, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():  # type: ignore[operator]  # noqa: ASYNC110 - polls fake state
            await asyncio.sleep(0.005)


def _segment(text: str, sequence: int = 0) -> DeliveredSegment:
    return DeliveredSegment(sequence, text, HI)


async def test_audio_after_interruption_never_reaches_the_audio_source() -> None:
    tts = MockTtsAdapter(frames_per_segment=8, pause_when=lambda _r: True, honor_cancel=False)
    rig = await build(tts)
    sink = rig.gateway.sink

    await rig.speech.enqueue(_segment("Ek lambi baat jo beech mein rukegi."))
    await _until(lambda: len(sink.captured) >= 1)
    heard_before = len(sink.captured)
    rig.fence.advance()  # interruption step 2: the fence first
    await rig.speech.cancel()
    outcome = await rig.speech.drain()
    await asyncio.sleep(0.02)

    assert len(sink.captured) == heard_before == 1
    assert sink.cleared >= 1  # AudioSource.clear_queue()
    [track] = outcome.tracks
    assert track.late_frames + track.stale_frames == 7
    assert rig.playback() == ["started", "cancelled"]
    assert outcome.spoken_text == "Ek lambi baat jo beech mein rukegi."
    assert not track.playback_completed
    await rig.speech.close()


async def test_completed_segments_play_out_in_order_then_complete() -> None:
    rig = await build(MockTtsAdapter(frames_per_segment=3))

    await rig.speech.enqueue(_segment("Pehli baat.", 0))
    await rig.speech.enqueue(_segment("Doosri baat.", 1))
    outcome = await rig.speech.drain()

    assert len(rig.gateway.sink.captured) == 6
    assert rig.playback() == ["started", "completed", "started", "completed"]
    assert [t.playback_completed for t in outcome.tracks] == [True, True]
    assert outcome.first_audio_ms is not None
    assert outcome.synthesized_text == "Pehli baat. Doosri baat."
    await rig.speech.close()
    await rig.speech.close()


async def test_oversized_prepared_text_becomes_bounded_pieces() -> None:
    tts = MockTtsAdapter(frames_per_segment=1)
    rig = await build(tts)
    text = ("Yeh e.g. ek lambi line hai, " * 18).strip()

    await rig.speech.enqueue(_segment(text))
    outcome = await rig.speech.drain()

    assert len(outcome.tracks) >= 2
    assert all(len(r.text) <= MAX_TTS_SEGMENT_CHARS for r in tts.requests)
    assert len({t.segment_id for t in outcome.tracks}) == len(outcome.tracks)
    assert len(outcome.spoken_segments) == 1  # one original segment heard
    await rig.speech.close()


async def test_unspeakable_segments_are_counted_not_synthesized() -> None:
    tts = MockTtsAdapter()
    rig = await build(tts)

    await rig.speech.enqueue(_segment("... ?!"))
    outcome = await rig.speech.drain()

    assert rig.speech.unspeakable_segments == 1
    assert tts.requests == []
    assert outcome.tracks == ()
    await rig.speech.close()


async def test_a_failed_piece_stops_the_rest_of_the_turn() -> None:
    tts = MockTtsAdapter(fail_when=lambda r: r.text.startswith("Pehli"))
    rig = await build(tts)

    await rig.speech.enqueue(_segment("Pehli baat.", 0))
    await rig.speech.enqueue(_segment("Doosri baat.", 1))
    outcome = await rig.speech.drain()

    assert outcome.failure is not None
    # The mock failure is transient and nothing played: retried up to the policy limit.
    assert [r.text for r in tts.requests] == ["Pehli baat."] * 3
    assert len(outcome.tracks[0].operation_ids) == 3
    assert outcome.tracks[1].skipped
    assert rig.gateway.sink.captured == []
    await rig.speech.close()


async def test_segment_queue_applies_backpressure() -> None:
    tts = MockTtsAdapter(frames_per_segment=2, pause_when=lambda r: r.sequence == 0)
    rig = await build(tts, queued=1)

    await rig.speech.enqueue(_segment("Ek.", 0))
    await _until(lambda: len(tts.requests) == 1)
    await rig.speech.enqueue(_segment("Do.", 1))
    blocked = asyncio.ensure_future(rig.speech.enqueue(_segment("Teen.", 2)))
    await asyncio.sleep(0.02)

    assert not blocked.done()  # bounded: the producer is busy with the paused first piece
    rig.fence.advance()
    await rig.speech.cancel()
    await asyncio.wait_for(blocked, 1)
    await rig.speech.drain()
    await rig.speech.close()


async def test_speech_needs_a_turn_stamp_and_drains_when_idle() -> None:
    rig = await build(MockTtsAdapter())
    with pytest.raises(ValueError, match="turn stamp"):
        SpeechTurn(rig.speech._deps, turn_stamp=rig.fence.stamp())

    outcome = await rig.speech.drain()

    assert outcome.tracks == ()
    assert outcome.accuracy.value == "unavailable"
    await rig.speech.close()
