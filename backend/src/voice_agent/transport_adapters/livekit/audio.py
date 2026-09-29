"""SDK-independent audio paths of the worker transport (docs/06 §9-§10).

Intake: browser microphone PCM (48 kHz mono Linear16 from the audio stream)
is rebuffered into exact 20 ms frames, validated as the internal 48 kHz
contract frame, resampled **once** to 16 kHz, rebuffered into exact 20 ms
(320-sample) frames on a monotonic timeline, and fanned out unchanged to
every consumer (local VAD and STT) through bounded per-consumer queues.

Publication: every agent frame passes the playback gate (current worker
generation, cancellation generation not already cleared) before it reaches
the 24 kHz ``AudioSource`` sink; other sample rates are converted inside the
adapter. ``clear`` revokes all generations seen so far and flushes the sink
queue (``AudioSource.clear_queue()``), so stale or interrupted speech is
never replayed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from voice_agent.contracts.audio import (
    BYTES_PER_SAMPLE,
    MILLISECONDS_PER_SECOND,
    RECOMMENDED_FRAME_MS,
    TTS_SAMPLE_RATE_HZ,
    VAD_SAMPLE_RATE_HZ,
    WEBRTC_SAMPLE_RATE_HZ,
    AudioFrame,
)
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackFrame
from voice_agent.ports.clock import Clock

FRAME_MS: Final = RECOMMENDED_FRAME_MS
PUBLICATION_SAMPLE_RATE_HZ: Final = TTS_SAMPLE_RATE_HZ
DEFAULT_FANOUT_CAPACITY: Final = 50  # one second of 20 ms frames per consumer


def frame_bytes(sample_rate_hz: int) -> int:
    return sample_rate_hz * FRAME_MS // MILLISECONDS_PER_SECOND * BYTES_PER_SAMPLE


class PcmResampler(Protocol):
    """Mono Linear16 resampler; output length may vary per push (internal latency)."""

    def push(self, pcm: bytes) -> bytes: ...

    def flush(self) -> bytes: ...


class AudioSink(Protocol):
    """The 24 kHz mono publication source (``AudioSource(queue_size_ms=200)``)."""

    async def capture(self, pcm: bytes, *, samples: int) -> None: ...

    def clear_queue(self) -> None: ...

    async def wait_for_playout(self) -> None:
        """Return once queued audio has played out (or the queue was cleared)."""
        ...

    @property
    def queued_ms(self) -> float: ...

    async def aclose(self) -> None: ...


@dataclass
class PcmRebuffer:
    """Splits a byte stream into exact ``frame_bytes`` chunks, carrying the remainder."""

    frame_bytes: int
    _buffer: bytearray = field(default_factory=bytearray)

    def push(self, pcm: bytes) -> list[bytes]:
        self._buffer += pcm
        whole = len(self._buffer) // self.frame_bytes * self.frame_bytes
        chunks = [
            bytes(self._buffer[i : i + self.frame_bytes]) for i in range(0, whole, self.frame_bytes)
        ]
        del self._buffer[:whole]
        return chunks

    @property
    def pending(self) -> int:
        return len(self._buffer)


class MicrophoneIntake:
    def __init__(
        self,
        *,
        session_id: str,
        resampler: PcmResampler,
        clock: Clock,
        track_label: str = "microphone",
    ) -> None:
        self._session_id = session_id
        self._resampler = resampler
        self._clock = clock
        self._label = track_label
        self._capture = PcmRebuffer(frame_bytes(WEBRTC_SAMPLE_RATE_HZ))
        self._output = PcmRebuffer(frame_bytes(VAD_SAMPLE_RATE_HZ))
        self._next_48k_ms = 0
        self._next_16k_ms = 0
        self._sequence = 0
        self.normalized_frames = 0

    def _normalized(self, pcm: bytes) -> AudioFrame:
        at_ms = max(self._clock.monotonic_ms(), self._next_48k_ms)
        frame = AudioFrame(
            session_id=self._session_id,
            track_label=self._label,
            sequence=self.normalized_frames,
            sample_rate_hz=WEBRTC_SAMPLE_RATE_HZ,
            duration_ms=FRAME_MS,
            captured_at_ms=at_ms,
            pcm=pcm,
        )
        self._next_48k_ms = frame.ends_at_ms
        self.normalized_frames += 1
        return frame

    def _vad_frame(self, pcm: bytes, source_ms: int) -> AudioFrame:
        at_ms = max(self._next_16k_ms, source_ms)
        frame = AudioFrame(
            session_id=self._session_id,
            track_label=self._label,
            sequence=self._sequence,
            sample_rate_hz=VAD_SAMPLE_RATE_HZ,
            duration_ms=FRAME_MS,
            captured_at_ms=at_ms,
            pcm=pcm,
        )
        self._sequence += 1
        self._next_16k_ms = frame.ends_at_ms
        return frame

    def accept(self, pcm_48k: bytes) -> tuple[AudioFrame, ...]:
        """Accept captured 48 kHz PCM; return the 16 kHz frames now complete."""
        out: list[AudioFrame] = []
        for chunk in self._capture.push(pcm_48k):
            normalized = self._normalized(chunk)
            resampled = self._resampler.push(normalized.pcm)
            out.extend(
                self._vad_frame(piece, normalized.captured_at_ms)
                for piece in self._output.push(resampled)
            )
        return tuple(out)


_CLOSED: Final = None


class AudioFanout:
    """Delivers the same 16 kHz frame object to every subscriber (bounded, drop-oldest)."""

    def __init__(self, capacity: int = DEFAULT_FANOUT_CAPACITY) -> None:
        self._capacity = capacity
        self._queues: list[asyncio.Queue[AudioFrame | None]] = []
        self._closed = False
        self.dropped = 0

    def subscribe(self) -> AsyncIterator[AudioFrame]:
        queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue(self._capacity + 1)
        if self._closed:
            queue.put_nowait(_CLOSED)
        self._queues.append(queue)
        return self._consume(queue)

    async def _consume(self, queue: asyncio.Queue[AudioFrame | None]) -> AsyncIterator[AudioFrame]:
        try:
            while (frame := await queue.get()) is not _CLOSED:
                yield frame
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    def publish(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        for queue in self._queues:
            if queue.qsize() >= self._capacity:
                queue.get_nowait()
                self.dropped += 1
            queue.put_nowait(frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for queue in self._queues:
            queue.put_nowait(_CLOSED)


class PlaybackGate:
    """Output authorization by worker generation and cleared cancellation generations."""

    def __init__(self, *, worker_generation: int) -> None:
        self._worker_generation = worker_generation
        self._floor = 0
        self._highest_seen = -1
        self._revoked = False

    def authorize(self, identity: PlaybackAckIdentity) -> bool:
        if self._revoked or identity.worker_generation != self._worker_generation:
            return False
        if identity.cancellation_generation < self._floor:
            return False
        self._highest_seen = max(self._highest_seen, identity.cancellation_generation)
        return True

    def clear(self) -> None:
        self._floor = max(self._floor, self._highest_seen + 1)

    def revoke(self) -> None:
        self._revoked = True


# Agent audio counts as "playing" until this long after the last queued frame
# ends, covering the browser jitter buffer/playout delay (echo-suppression hook).
PLAYBACK_TAIL_MS: Final = 300


class PlaybackActivity:
    """Echo/self-interruption suppression hook: is agent audio (probably) audible now?

    The speech-activity detector raises its interruption threshold (0.7 vs
    0.5) while this reports active; the transport never decides speech itself.
    """

    def __init__(self, *, tail_ms: int = PLAYBACK_TAIL_MS) -> None:
        self._tail_ms = tail_ms
        self._audible_until_ms: int | None = None

    def mark_published(self, *, now_ms: int, duration_ms: int) -> None:
        start = max(now_ms, self._audible_until_ms or now_ms)
        self._audible_until_ms = start + duration_ms

    def clear(self) -> None:
        self._audible_until_ms = None

    def active(self, now_ms: int) -> bool:
        until = self._audible_until_ms
        return until is not None and now_ms < until + self._tail_ms


ResamplerFactory = Callable[[int], PcmResampler]


class AgentAudioPublisher:
    def __init__(
        self,
        *,
        gate: PlaybackGate,
        sink: AudioSink | None,
        resampler_factory: ResamplerFactory,
    ) -> None:
        self._gate = gate
        self._sink = sink
        self._factory = resampler_factory
        self._converters: dict[int, PcmResampler] = {}
        self.published = 0
        self.stale_dropped = 0

    def attach(self, sink: AudioSink | None) -> None:
        self._sink = sink

    def _publication_pcm(self, frame: AudioFrame) -> bytes:
        rate = frame.sample_rate_hz
        if rate == PUBLICATION_SAMPLE_RATE_HZ:
            return frame.pcm
        converter = self._converters.get(rate)
        if converter is None:
            converter = self._converters[rate] = self._factory(rate)
        return converter.push(frame.pcm)

    async def publish(self, frame: PlaybackFrame) -> bool:
        sink = self._sink
        if sink is None or not self._gate.authorize(frame.identity):
            self.stale_dropped += 1
            return False
        pcm = self._publication_pcm(frame.frame)
        samples = len(pcm) // BYTES_PER_SAMPLE
        if samples:
            await sink.capture(pcm[: samples * BYTES_PER_SAMPLE], samples=samples)
        self.published += 1
        return True

    def clear(self) -> None:
        self._gate.clear()
        if self._sink is not None:
            self._sink.clear_queue()

    async def wait_for_playout(self, *, timeout_s: float) -> bool:
        """``True`` when the queue drained within ``timeout_s``; no sink counts as drained."""
        sink = self._sink
        if sink is None:
            return True
        try:
            async with asyncio.timeout(timeout_s):
                await sink.wait_for_playout()
        except TimeoutError:
            return False
        return True
