"""Opt-in live Sarvam Bulbul v3 checks (``-m sarvam``; metered in INR; WP9 budget INR 50.00).

Gated three ways by ``tests/conftest.py``: the ``sarvam`` marker must be
selected explicitly, ``VOICE_AGENT_SECRETS_FILE`` must be set, and
``VOICE_AGENT_SARVAM_LIVE_APPROVED=1`` must be set (docs/15 §2.5 records the
approved budget). Scripted synthetic text only; the key loads only through
the WP3 loader/resolver and is never printed. Every submitted character is
charged to :data:`tests.support.sarvam_probe.SPEND` (INR 3.00/1,000 chars)
and the run refuses to exceed its INR 5.00 guard. No audio is stored.

Printed evidence (``-s``): first-audio/total ms, generated audio ms, real-time
factor, frame counts, chunk shapes, cancellation outcome, and INR spent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr
from tests.support.fake_livekit import Decimate, FakeGateway, FakeSink
from tests.support.sarvam_probe import SPEND, RecordingConnector, live_settings

from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_output import SpeechDeps, SpeechTurn
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.enums import OperationStatus, TtsLanguageCode
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsSegmentCompleted,
    TtsSegmentRequest,
    TtsVoiceConfig,
)
from voice_agent.contracts.usage import UsageUnit
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.events_and_latency.clock import ManualClock, SystemClock, UuidIdGenerator
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.orchestration.response_generation import DeliveredSegment
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord
from voice_agent.provider_registry.tts_check_config import SARVAM_CREDENTIAL_REF
from voice_agent.response_segmentation.tts_text import prepare_tts_text
from voice_agent.security.credentials import CredentialError, CredentialResolver
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport
from voice_agent.tts_adapters.sarvam.adapter import SarvamTtsAdapter

pytestmark = [pytest.mark.sarvam, pytest.mark.asyncio]
SESSION = "00000000-0000-4000-8000-0000000009a1"
TURN = "00000000-0000-4000-8000-0000000009a2"
VOICE = TtsVoiceConfig(provider="sarvam", model="bulbul:v3", voice_id="priya")
SAMPLES = (
    ("hindi", TtsLanguageCode.HI_IN, "नमस्ते! मैं आपकी कैसे मदद कर सकती हूँ?"),
    ("hinglish", TtsLanguageCode.HI_IN, "Aapki fees Rs. 125000 hai, aur OTP 482913 hai."),
    ("english", TtsLanguageCode.EN_IN, "Hello! Your appointment is at 10:30 AM, e.g. tomorrow."),
)
LONG = (
    "Yeh ek lamba jawab hai jise hum beech mein hi rok denge, taaki cancellation ka "
    "proof mil sake. Iske baad koi purana audio agle turn mein nahi aana chahiye."
)


def _api_key() -> SecretStr:
    try:
        return CredentialResolver(live_settings()).resolve(SARVAM_CREDENTIAL_REF)
    except CredentialError:
        pytest.skip("SARVAM_API_KEY is not configured in the secrets file")


def _request(text: str, language: TtsLanguageCode, segment: str) -> TtsSegmentRequest:
    return TtsSegmentRequest(
        stamp=GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id="00000000-0000-4000-8000-0000000009a3",
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id="00000000-0000-4000-8000-0000000009a4",
        segment_id=segment,
        sequence=0,
        text=text,
        language_code=language,
    )


async def _timed(
    adapter: SarvamTtsAdapter, request: TtsSegmentRequest
) -> tuple[list[TtsEvent], int, int]:
    started = time.monotonic()
    first_ms: int | None = None
    events: list[TtsEvent] = []
    async for event in adapter.synthesize(request):
        if first_ms is None and isinstance(event, TtsAudioChunk):
            first_ms = round((time.monotonic() - started) * 1000)
        events.append(event)
    return events, first_ms or -1, round((time.monotonic() - started) * 1000)


async def test_bulbul_v3_priya_speaks_hindi_hinglish_and_english() -> None:
    connector = RecordingConnector(_api_key())
    adapter = SarvamTtsAdapter(connector, clock=SystemClock())
    await adapter.open_session(VOICE)  # prewarms the connection
    try:
        for index, (label, language, raw) in enumerate(SAMPLES):
            text = prepare_tts_text(raw, language).normalized
            segment = f"00000000-0000-4000-8000-0000000009b{index}"
            events, first_ms, total_ms = await _timed(adapter, _request(text, language, segment))

            frames = [e for e in events if isinstance(e, TtsAudioChunk)]
            completed = events[-1]
            assert isinstance(completed, TtsSegmentCompleted), label
            assert frames, label
            assert all(f.frame.sample_rate_hz == 24_000 for f in frames)
            assert all(f.frame.duration_ms == 20 for f in frames)
            assert completed.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) == len(text)
            assert first_ms < 5000, f"{label}: first audio {first_ms} ms"
            audio_ms = len(frames) * 20
            print(
                f"[{label}] lang={language.value} chars={len(text)} text={text!a} "
                f"first_audio_ms={first_ms} total_ms={total_ms} audio_ms={audio_ms} "
                f"rtf={total_ms / audio_ms:.2f} frames={len(frames)} "
                f"request_id={'yes' if completed.provider_request_id else 'no'}"
            )
    finally:
        await adapter.close()
    shape = connector.shapes[0]
    assert adapter.counters.get("connections_opened") == 1  # one warm connection, reused
    print(
        f"[shape] content_types={sorted(shape.content_types)} chunks={len(shape.chunk_bytes)} "
        f"first_chunks={shape.chunk_bytes[:4]} finals={shape.finals} "
        f"spent_inr={SPEND.spent_inr} chars={SPEND.characters}"
    )


async def test_mid_stream_cancellation_discards_the_connection_and_recovers() -> None:
    connector = RecordingConnector(_api_key())
    adapter = SarvamTtsAdapter(connector, clock=SystemClock(), prewarm=False)
    await adapter.open_session(VOICE)
    try:
        request = _request(LONG, TtsLanguageCode.HI_IN, "00000000-0000-4000-8000-0000000009c1")
        stream = adapter.synthesize(request)
        received = 0
        terminal: TtsEvent | None = None
        async for event in stream:
            if isinstance(event, TtsAudioChunk):
                received += 1
                if received == 10:
                    await adapter.cancel_segment(request.segment_id)
                continue
            terminal = event
            break
        await stream.aclose()
        assert isinstance(terminal, TtsCancelled)
        retry = _request(
            "Theek hai.", TtsLanguageCode.HI_IN, "00000000-0000-4000-8000-0000000009c2"
        )
        events, first_ms, _ = await _timed(adapter, retry)
        assert isinstance(events[-1], TtsSegmentCompleted)
    finally:
        await adapter.close()
    assert adapter.counters.get("connections_discarded", 0) >= 1
    assert adapter.counters.get("connections_opened") == 2
    print(
        f"[cancel] frames_before_cancel={received} cancelled_usage="
        f"{terminal.usage.reporting_status.value} next_first_audio_ms={first_ms} "
        f"spent_inr={SPEND.spent_inr}"
    )


class PacedSink(FakeSink):
    """Plays out in real time like ``rtc.AudioSource`` (24 kHz, ~200 ms queue)."""

    async def capture(self, pcm: bytes, *, samples: int) -> None:
        await super().capture(pcm, samples=samples)
        await asyncio.sleep(samples / 24_000)


@dataclass
class _Log:
    records: list[EventRecord] = field(default_factory=list)

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        return None


async def test_real_speech_reaches_the_audio_source_and_stops_at_interruption() -> None:
    gateway = FakeGateway(sink=PacedSink())
    gateway.present.add("va-user-live")
    transport = LiveKitSessionTransport(
        session_id=SESSION,
        browser_identity="va-user-live",
        agent_identity="va-agent-live",
        worker_generation=1,
        gateway=gateway,
        clock=ManualClock(),
        resampler_factory=Decimate,
    )
    await transport.connect()
    clock, ids, log = SystemClock(), UuidIdGenerator(), _Log()
    operations = InMemoryOperationRepository()
    fence = GenerationFence(session_id=SESSION, worker_generation=1)
    fence.activate_turn(TURN)
    adapter = SarvamTtsAdapter(RecordingConnector(_api_key()), clock=clock)
    await adapter.open_session(VOICE)
    speech = SpeechTurn(
        SpeechDeps(
            setup=SpeechSetup(
                session_id=SESSION,
                worker_generation=1,
                provider="sarvam",
                model="bulbul:v3",
                voice_id="priya",
                ack_grace_ms=0,
            ),
            tts=adapter,
            transport=transport,
            fence=fence,
            evidence=SttEvidence(
                EvidenceContext(
                    SESSION, "wp9-live", SESSION, AgentConfigEnvironment.DEVELOPMENT, 1
                ),
                turns=InMemoryTurnRepository(),
                operations=operations,
                events=log,
                costs=log,
                rate_card=phase0_rate_card(),
                clock=clock,
                ids=ids,
            ),
            publisher=RealtimePublisher(
                transport, session_id=SESSION, correlation_id="wp9-live", clock=clock, ids=ids
            ),
            clock=clock,
            ids=ids,
        ),
        turn_stamp=fence.stamp(turn_id=TURN),
    )
    sink = gateway.sink
    try:
        await speech.enqueue(
            DeliveredSegment(0, "Namaste! Main Priya hoon.", TtsLanguageCode.HI_IN)
        )
        await speech.enqueue(DeliveredSegment(1, LONG, TtsLanguageCode.HI_IN))
        async with asyncio.timeout(10):
            while not any(t.playback_started for t in speech.tracks[1:]):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        heard = len(sink.captured)
        fence.advance()  # interruption: fence first, then cancel
        await speech.cancel()
        outcome = await speech.drain()
        await asyncio.sleep(0.5)
    finally:
        await speech.close()
        await adapter.close()
        await transport.close()

    assert len(sink.captured) == heard  # nothing after the interruption reached the source
    assert sink.cleared >= 1
    first, second = outcome.tracks
    assert second.late_frames + second.stale_frames > 0  # buffered audio was dropped
    assert first.playback_completed
    assert second.playback_started
    assert not second.playback_completed
    states = [s.body["payload"]["state"] for s in gateway.sent if s.topic == "va.playback.v1"]
    assert states == ["started", "completed", "started", "cancelled"]
    rows = await operations.list_for_session(SESSION)
    assert {o.status for o in rows} >= {OperationStatus.SUCCEEDED}
    print(
        f"[pipeline] frames_to_audio_source={heard} first_audio_ms={outcome.first_audio_ms} "
        f"late_or_stale_dropped={second.late_frames + second.stale_frames} "
        f"op_first_audio_ms={[o.time_to_first_result_ms for o in rows]} "
        f"op_status={[o.status.value for o in rows]} spent_inr={SPEND.spent_inr} "
        f"chars={SPEND.characters}"
    )
