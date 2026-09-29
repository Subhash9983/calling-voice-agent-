"""Microphone intake (48 kHz/20 ms -> one 16 kHz resample -> fan-out) and playback gating.

docs/06 §9-§10. The fake resampler decimates by three so frame accounting is
exact; the real SoX resampler and ``AudioSource`` are covered offline in
``test_rtc_audio.py``.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from voice_agent.contracts.audio import (
    TTS_SAMPLE_RATE_HZ,
    VAD_SAMPLE_RATE_HZ,
    WEBRTC_SAMPLE_RATE_HZ,
    AudioFrame,
    square_wave_pcm,
)
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackFrame
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.transport_adapters.livekit.audio import (
    FRAME_MS,
    AgentAudioPublisher,
    AudioFanout,
    MicrophoneIntake,
    PcmRebuffer,
    PlaybackGate,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SEGMENT_ID = "00000000-0000-4000-8000-000000000002"
TURN_ID = "00000000-0000-4000-8000-000000000003"
SAMPLES_48K = WEBRTC_SAMPLE_RATE_HZ * FRAME_MS // 1000


class Decimate:
    """Deterministic 3:1 decimation (test double for the SoX resampler)."""

    def __init__(self) -> None:
        self.pushed = 0

    def push(self, pcm: bytes) -> bytes:
        self.pushed += 1
        samples = [pcm[i : i + 2] for i in range(0, len(pcm), 2)]
        return b"".join(samples[::3])

    def flush(self) -> bytes:
        return b""


class Upsample:
    def __init__(self, factor_num: int, factor_den: int) -> None:
        self._num, self._den = factor_num, factor_den

    def push(self, pcm: bytes) -> bytes:
        samples = [pcm[i : i + 2] for i in range(0, len(pcm), 2)]
        count = len(samples) * self._num // self._den
        return b"".join(samples[i * self._den // self._num] for i in range(count))

    def flush(self) -> bytes:
        return b""


class FakeSink:
    def __init__(self) -> None:
        self.captured: list[int] = []
        self.cleared = 0
        self.closed = False

    async def capture(self, pcm: bytes, *, samples: int) -> None:
        self.captured.append(samples)

    def clear_queue(self) -> None:
        self.cleared += 1
        self.captured.clear()

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(3600 if self.captured and self.stalled else 0)

    stalled = False

    @property
    def queued_ms(self) -> float:
        return len(self.captured) * 20.0

    async def aclose(self) -> None:
        self.closed = True


def test_rebuffer_emits_exact_frames_and_keeps_remainder() -> None:
    rebuffer = PcmRebuffer(frame_bytes=4)

    assert rebuffer.push(b"abcdef") == [b"abcd"]
    assert rebuffer.push(b"gh") == [b"efgh"]
    assert rebuffer.pending == 0


def test_intake_normalizes_48k_and_resamples_once_to_16k_20ms() -> None:
    clock = ManualClock(monotonic_ms=1000)
    resampler = Decimate()
    intake = MicrophoneIntake(session_id=SESSION_ID, resampler=resampler, clock=clock)

    frames: list[AudioFrame] = []
    for _ in range(5):
        frames += intake.accept(square_wave_pcm(0.5, SAMPLES_48K))
        clock.advance(FRAME_MS)

    assert resampler.pushed == 5  # exactly one resample per normalized 48 kHz frame
    assert len(frames) == 5
    assert {f.sample_rate_hz for f in frames} == {VAD_SAMPLE_RATE_HZ}
    assert {f.duration_ms for f in frames} == {FRAME_MS}
    assert [f.sequence for f in frames] == [0, 1, 2, 3, 4]
    starts = [f.captured_at_ms for f in frames]
    assert starts == sorted(starts)
    assert all(b.captured_at_ms >= a.ends_at_ms for a, b in itertools.pairwise(frames))
    assert intake.normalized_frames == 5


def test_intake_rebuffers_irregular_capture_chunks() -> None:
    intake = MicrophoneIntake(session_id=SESSION_ID, resampler=Decimate(), clock=ManualClock())
    pcm = square_wave_pcm(0.2, SAMPLES_48K * 3)

    frames = intake.accept(pcm[:1000]) + intake.accept(pcm[1000:5000]) + intake.accept(pcm[5000:])

    assert len(frames) == 3
    assert intake.normalized_frames == 3


def test_intake_timestamps_stay_monotonic_when_the_clock_stalls() -> None:
    intake = MicrophoneIntake(session_id=SESSION_ID, resampler=Decimate(), clock=ManualClock())

    frames = [f for _ in range(3) for f in intake.accept(bytes(SAMPLES_48K * 2))]

    assert [f.captured_at_ms for f in frames] == [0, 20, 40]


def _frame(sequence: int) -> AudioFrame:
    return AudioFrame(
        session_id=SESSION_ID,
        sequence=sequence,
        sample_rate_hz=VAD_SAMPLE_RATE_HZ,
        duration_ms=FRAME_MS,
        captured_at_ms=sequence * FRAME_MS,
        pcm=bytes(640),
    )


async def _collect(iterator: object, count: int) -> list[AudioFrame]:
    items: list[AudioFrame] = []
    async for frame in iterator:  # type: ignore[attr-defined]
        items.append(frame)
        if len(items) == count:
            break
    return items


@pytest.mark.asyncio
async def test_fanout_delivers_the_same_frame_to_every_consumer() -> None:
    fanout = AudioFanout(capacity=10)
    vad, stt = fanout.subscribe(), fanout.subscribe()
    frames = [_frame(i) for i in range(3)]

    for frame in frames:
        fanout.publish(frame)
    got_vad, got_stt = await asyncio.gather(_collect(vad, 3), _collect(stt, 3))

    assert got_vad == frames
    assert all(a is b for a, b in zip(got_vad, got_stt, strict=True))


@pytest.mark.asyncio
async def test_fanout_bounds_each_consumer_by_dropping_oldest() -> None:
    fanout = AudioFanout(capacity=2)
    slow = fanout.subscribe()
    for i in range(5):
        fanout.publish(_frame(i))
    fanout.close()

    received = [frame async for frame in slow]

    assert [f.sequence for f in received] == [3, 4]
    assert fanout.dropped == 3


@pytest.mark.asyncio
async def test_fanout_close_ends_current_and_future_consumers() -> None:
    fanout = AudioFanout(capacity=2)
    current = fanout.subscribe()
    fanout.close()
    late = fanout.subscribe()

    assert [f async for f in current] == []
    assert [f async for f in late] == []


def _identity(worker: int = 1, cancellation: int = 0) -> PlaybackAckIdentity:
    return PlaybackAckIdentity(
        worker_generation=worker, cancellation_generation=cancellation, segment_id=SEGMENT_ID
    )


def _playback(rate: int = TTS_SAMPLE_RATE_HZ, **identity: int) -> PlaybackFrame:
    samples = rate * FRAME_MS // 1000
    return PlaybackFrame(
        identity=_identity(**identity),
        turn_id=TURN_ID,
        frame=AudioFrame(
            session_id=SESSION_ID,
            track_label="agent",
            sequence=0,
            sample_rate_hz=rate,
            duration_ms=FRAME_MS,
            captured_at_ms=0,
            pcm=bytes(samples * 2),
        ),
    )


def test_gate_rejects_other_worker_generations() -> None:
    gate = PlaybackGate(worker_generation=2)

    assert gate.authorize(_identity(worker=2))
    assert not gate.authorize(_identity(worker=1))


def test_gate_clear_blocks_every_generation_seen_so_far() -> None:
    gate = PlaybackGate(worker_generation=1)
    assert gate.authorize(_identity(cancellation=3))

    gate.clear()

    assert not gate.authorize(_identity(cancellation=3))
    assert not gate.authorize(_identity(cancellation=2))
    assert gate.authorize(_identity(cancellation=4))


def test_revoked_gate_authorizes_nothing() -> None:
    gate = PlaybackGate(worker_generation=1)
    gate.revoke()

    assert not gate.authorize(_identity(cancellation=99))


@pytest.mark.asyncio
async def test_publisher_writes_24k_frames_and_counts_stale_drops() -> None:
    sink = FakeSink()
    gate = PlaybackGate(worker_generation=1)
    publisher = AgentAudioPublisher(gate=gate, sink=sink, resampler_factory=lambda _r: Decimate())

    assert await publisher.publish(_playback())
    assert not await publisher.publish(_playback(worker=9))

    assert sink.captured == [TTS_SAMPLE_RATE_HZ * FRAME_MS // 1000]
    assert publisher.published == 1
    assert publisher.stale_dropped == 1


@pytest.mark.asyncio
async def test_publisher_converts_other_rates_to_the_publication_format() -> None:
    sink = FakeSink()
    publisher = AgentAudioPublisher(
        gate=PlaybackGate(worker_generation=1),
        sink=sink,
        resampler_factory=lambda rate: Upsample(TTS_SAMPLE_RATE_HZ, rate),
    )

    await publisher.publish(_playback(rate=VAD_SAMPLE_RATE_HZ))

    assert sink.captured == [TTS_SAMPLE_RATE_HZ * FRAME_MS // 1000]


@pytest.mark.asyncio
async def test_accepted_interruption_flushes_buffered_audio_and_stale_frames() -> None:
    sink = FakeSink()
    publisher = AgentAudioPublisher(
        gate=PlaybackGate(worker_generation=1), sink=sink, resampler_factory=lambda _r: Decimate()
    )
    for _ in range(5):
        await publisher.publish(_playback(cancellation=1))

    publisher.clear()
    late = await publisher.publish(_playback(cancellation=1))
    fresh = await publisher.publish(_playback(cancellation=2))

    assert sink.cleared == 1
    assert late is False
    assert fresh is True
    assert sink.captured == [TTS_SAMPLE_RATE_HZ * FRAME_MS // 1000]


@pytest.mark.asyncio
async def test_publisher_without_sink_drops_everything() -> None:
    publisher = AgentAudioPublisher(
        gate=PlaybackGate(worker_generation=1), sink=None, resampler_factory=lambda _r: Decimate()
    )

    assert not await publisher.publish(_playback())
    publisher.clear()


@pytest.mark.asyncio
async def test_playout_wait_is_bounded_and_trivial_without_a_sink() -> None:
    sink = FakeSink()
    sink.stalled = True
    stuck = AgentAudioPublisher(
        gate=PlaybackGate(worker_generation=1), sink=sink, resampler_factory=lambda _r: Decimate()
    )
    await stuck.publish(_playback())
    empty = AgentAudioPublisher(
        gate=PlaybackGate(worker_generation=1), sink=None, resampler_factory=lambda _r: Decimate()
    )

    assert await stuck.wait_for_playout(timeout_s=0.05) is False
    assert await empty.wait_for_playout(timeout_s=0.05) is True
