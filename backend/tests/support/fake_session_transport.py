"""In-process ``SessionTransportPort`` fake that fans each fed 16 kHz frame to every subscriber."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from voice_agent.contracts.audio import AudioFrame, square_wave_pcm
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.realtime_wire import encode_agent_message
from voice_agent.contracts.transport import (
    ClientEvent,
    PlaybackFrame,
    RealtimeTopic,
    TransportEvent,
    TransportUsage,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"


def mic_frame(sequence: int, level: float, session_id: str = SESSION_ID) -> AudioFrame:
    return AudioFrame(
        session_id=session_id,
        sequence=sequence,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=sequence * 20,
        pcm=square_wave_pcm(level, 320),
    )


@dataclass(frozen=True)
class Sent:
    topic: str
    reliable: bool
    body: dict[str, Any]


class FakeSessionTransport:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[AudioFrame | None]] = []
        self._client: asyncio.Queue[ClientEvent | None] = asyncio.Queue()
        self._lifecycle: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self.sent: list[Sent] = []
        self._subscribed = asyncio.Event()
        self.agent_audio_active = False
        self.closed = False
        # Agent audio evidence (WP9): what reached "playback", in order.
        self.published: list[PlaybackFrame] = []
        self.finished: list[PlaybackAckIdentity] = []
        self.clears = 0
        self.playouts = 0
        # When set, ``wait_for_playout`` blocks until released (or cleared).
        self.hold_playout: asyncio.Event | None = None
        self.playout_reached = asyncio.Event()

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    async def wait_for_subscribers(self, count: int) -> None:
        while len(self._subscribers) < count:
            self._subscribed.clear()
            await self._subscribed.wait()

    def feed(self, frame: AudioFrame) -> None:
        for queue in self._subscribers:
            queue.put_nowait(frame)

    async def speak(self, first: int, count: int, level: float) -> int:
        for sequence in range(first, first + count):
            self.feed(mic_frame(sequence, level))
            await asyncio.sleep(0)
        return first + count

    def client(self, event: ClientEvent) -> None:
        self._client.put_nowait(event)

    def audio_frames(self) -> AsyncIterator[AudioFrame]:
        queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._subscribers.append(queue)
        self._subscribed.set()
        return _drain(queue)

    def client_events(self) -> AsyncIterator[ClientEvent]:
        return _drain(self._client)

    def lifecycle_events(self) -> AsyncIterator[TransportEvent]:
        return _drain(self._lifecycle)

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        body = json.loads(encode_agent_message(envelope, reliable=reliable))
        self.sent.append(Sent(topic.value, reliable, body))

    def on(self, topic: str) -> list[Sent]:
        return [s for s in self.sent if s.topic == topic]

    async def connect(self) -> None:
        return None

    async def publish_audio(self, frame: PlaybackFrame) -> None:
        self.published.append(frame)
        await asyncio.sleep(0)

    async def finish_segment(self, identity: PlaybackAckIdentity) -> None:
        self.finished.append(identity)

    async def clear_playback(self) -> None:
        self.clears += 1
        if self.hold_playout is not None:
            self.hold_playout.set()

    async def wait_for_playout(self) -> None:
        self.playouts += 1
        self.playout_reached.set()
        if self.hold_playout is not None:
            await self.hold_playout.wait()

    def segments_published(self) -> list[str]:
        """Segment IDs in the order their first frame was published."""
        seen: list[str] = []
        for frame in self.published:
            if frame.identity.segment_id not in seen:
                seen.append(frame.identity.segment_id)
        return seen

    @property
    def browser_present(self) -> bool:
        return True

    def usage(self) -> TransportUsage:
        return TransportUsage()

    async def close(self) -> None:
        self.closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)


async def _drain[T](queue: asyncio.Queue[T | None]) -> AsyncIterator[T]:
    while (item := await queue.get()) is not None:
        yield item
