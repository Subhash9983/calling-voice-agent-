"""Deterministic in-process mock transport that also plays the browser's role.

The microphone is a script of synthetic 16 kHz frames (``Speak``/``Silence``)
with ``WaitUntil`` gates that pause input until a condition on observed
playback holds, so interruptions land mid-playback by event ordering rather
than wall-clock timing. Published agent audio is acknowledged like a
browser: ``started`` on a segment's first frame and ``completed`` on its end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from voice_agent.contracts.audio import (
    MILLISECONDS_PER_SECOND,
    RECOMMENDED_FRAME_MS,
    VAD_SAMPLE_RATE_HZ,
    AudioFrame,
    square_wave_pcm,
)
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import (
    ClientEvent,
    PlaybackAck,
    PlaybackAckKind,
    PlaybackFrame,
    RealtimeTopic,
)

_SAMPLES_PER_FRAME = VAD_SAMPLE_RATE_HZ * RECOMMENDED_FRAME_MS // MILLISECONDS_PER_SECOND
_CLIENT_EVENT_BUFFER = 256


@dataclass(frozen=True, slots=True)
class Speak:
    duration_ms: int
    amplitude: float = 0.6


@dataclass(frozen=True, slots=True)
class Silence:
    duration_ms: int


@dataclass(frozen=True, slots=True)
class WaitUntil:
    label: str
    condition: Callable[[MockTransport], bool]


@dataclass(frozen=True, slots=True)
class Signal:
    """Run a synchronous action when the script reaches this point."""

    label: str
    action: Callable[[], None]


@dataclass(frozen=True, slots=True)
class WaitForEvent:
    """Pause input until an external event is set (e.g. the LLM started streaming)."""

    label: str
    event: asyncio.Event


ScriptStep = Speak | Silence | WaitUntil | Signal | WaitForEvent


def _no_record(_entry: str) -> None:
    return None


class MockTransport:
    def __init__(
        self,
        session_id: str,
        script: Sequence[ScriptStep],
        *,
        auto_ack: bool = True,
        yield_per_frame: bool = True,
        playback_failure: bool = False,
        record: Callable[[str], None] = _no_record,
    ) -> None:
        self._session_id = session_id
        self._script = tuple(script)
        self._auto_ack = auto_ack
        self._yield_per_frame = yield_per_frame
        self._playback_failure = playback_failure
        self._record = record
        self._client_events: asyncio.Queue[ClientEvent | None] = asyncio.Queue(
            maxsize=_CLIENT_EVENT_BUFFER
        )
        self._waiters: list[asyncio.Future[None]] = []
        self._started_segments: set[str] = set()
        self._closed = False
        self.published: list[PlaybackFrame] = []
        self.finished_segments: list[PlaybackAckIdentity] = []
        self.sent_events: list[tuple[RealtimeTopic, EventEnvelope, bool]] = []
        self.clear_count = 0
        self.clear_marks: list[int] = []

    @property
    def started_segment_count(self) -> int:
        return len(self._started_segments)

    def sent_count(self, event_type: str) -> int:
        return sum(1 for _, envelope, _ in self.sent_events if envelope.event_type == event_type)

    def _notify(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _wait_until(self, step: WaitUntil) -> None:
        while not step.condition(self) and not self._closed:
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter

    def _frame(self, sequence: int, at_ms: int, amplitude: float) -> AudioFrame:
        return AudioFrame(
            session_id=self._session_id,
            sequence=sequence,
            sample_rate_hz=VAD_SAMPLE_RATE_HZ,
            duration_ms=RECOMMENDED_FRAME_MS,
            captured_at_ms=at_ms,
            pcm=square_wave_pcm(amplitude, _SAMPLES_PER_FRAME),
        )

    async def audio_frames(self) -> AsyncIterator[AudioFrame]:
        sequence = 0
        clock_ms = 0
        for step in self._script:
            if isinstance(step, WaitUntil):
                self._record(f"transport.wait:{step.label}")
                await self._wait_until(step)
                continue
            if isinstance(step, WaitForEvent):
                self._record(f"transport.wait:{step.label}")
                await step.event.wait()
                continue
            if isinstance(step, Signal):
                self._record(f"transport.signal:{step.label}")
                step.action()
                continue
            amplitude = step.amplitude if isinstance(step, Speak) else 0.0
            for _ in range(step.duration_ms // RECOMMENDED_FRAME_MS):
                if self._closed:
                    return
                yield self._frame(sequence, clock_ms, amplitude)
                sequence += 1
                clock_ms += RECOMMENDED_FRAME_MS
                if self._yield_per_frame:
                    await asyncio.sleep(0)

    async def client_events(self) -> AsyncIterator[ClientEvent]:
        while True:
            event = await self._client_events.get()
            if event is None:
                return
            yield event

    async def _ack(self, kind: PlaybackAckKind, identity: PlaybackAckIdentity) -> None:
        if self._auto_ack and not self._closed:
            await self._client_events.put(PlaybackAck(ack=kind, identity=identity))

    async def publish_audio(self, frame: PlaybackFrame) -> None:
        if self._closed:
            raise RuntimeError("transport is closed")
        self.published.append(frame)
        segment_id = frame.identity.segment_id
        if segment_id not in self._started_segments:
            self._started_segments.add(segment_id)
            await self._ack(PlaybackAckKind.STARTED, frame.identity)
        self._notify()

    async def finish_segment(self, identity: PlaybackAckIdentity) -> None:
        self.finished_segments.append(identity)
        kind = PlaybackAckKind.FAILED if self._playback_failure else PlaybackAckKind.COMPLETED
        await self._ack(kind, identity)
        self._notify()

    def inject_client_event(self, event: ClientEvent) -> None:
        """Simulate an arbitrary (possibly stale or forged) browser message."""
        self._client_events.put_nowait(event)

    async def clear_playback(self) -> None:
        self._record("transport.clear_playback")
        self.clear_count += 1
        self.clear_marks.append(len(self.published))
        self._notify()

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        self._record(f"transport.send_event:{envelope.event_type.value}")
        self.sent_events.append((topic, envelope, reliable))
        self._notify()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._record("transport.close")
        self._notify()
        await self._client_events.put(None)
