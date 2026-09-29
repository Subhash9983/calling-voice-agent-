"""In-process fake of the LiveKit room gateway seam (no SDK, no network).

Tests drive the room: participants join/leave, the browser microphone
produces 48 kHz PCM, data packets arrive, and reconnect/disconnect happen.
Published audio and data are recorded for assertions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from voice_agent.transport_adapters.livekit.gateway import (
    RoomDisconnectCause,
    RoomHandlers,
)

SAMPLES_48K_20MS = 960


class FakeSink:
    def __init__(self) -> None:
        self.captured: list[bytes] = []
        self.cleared = 0
        self.closed = False
        # Number of data packets already sent when each playout wait returned.
        self.drained_after: list[int] = []
        self.sent_ref: list[Any] = []
        self.stall = False

    async def wait_for_playout(self) -> None:
        if self.stall:
            await asyncio.sleep(3600)
        self.drained_after.append(len(self.sent_ref))

    async def capture(self, pcm: bytes, *, samples: int) -> None:
        self.captured.append(pcm)

    def clear_queue(self) -> None:
        self.cleared += 1

    @property
    def queued_ms(self) -> float:
        return 0.0

    async def aclose(self) -> None:
        self.closed = True


class Decimate:
    """Deterministic integer-ratio decimation standing in for the SoX resampler."""

    def __init__(self, input_rate: int, output_rate: int) -> None:
        self._step = max(1, input_rate // output_rate)
        self._repeat = max(1, output_rate // input_rate)

    def push(self, pcm: bytes) -> bytes:
        samples = [pcm[i : i + 2] for i in range(0, len(pcm), 2)]
        return b"".join(s * self._repeat for s in samples[:: self._step])

    def flush(self) -> bytes:
        return b""


@dataclass
class SentData:
    payload: bytes
    reliable: bool
    topic: str
    destination: str

    @property
    def body(self) -> dict[str, Any]:
        decoded: dict[str, Any] = json.loads(self.payload)
        return decoded


@dataclass
class FakeGateway:
    local_identity: str = "va-agent-1"
    present: set[str] = field(default_factory=set)
    handlers: RoomHandlers | None = None
    sink: FakeSink = field(default_factory=FakeSink)
    sent: list[SentData] = field(default_factory=list)
    connected: bool = False
    published: bool = False
    disconnects: int = 0
    fail_connect: Exception | None = None
    fail_publish_data: Exception | None = None
    fail_disconnect: Exception | None = None
    mic: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)

    def __post_init__(self) -> None:
        self.sink.sent_ref = self.sent

    async def connect(self, handlers: RoomHandlers) -> None:
        self.handlers = handlers
        if self.fail_connect is not None:
            raise self.fail_connect
        self.connected = True

    def remote_identities(self) -> frozenset[str]:
        return frozenset(self.present)

    async def publish_agent_audio(self) -> FakeSink:
        self.published = True
        return self.sink

    async def unpublish_agent_audio(self) -> None:
        self.published = False

    async def publish_data(
        self, payload: bytes, *, reliable: bool, topic: str, destination: str
    ) -> None:
        if self.fail_publish_data is not None:
            raise self.fail_publish_data
        self.sent.append(SentData(payload, reliable, topic, destination))

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self.fail_disconnect is not None:
            raise self.fail_disconnect
        self.connected = False
        if self.handlers is not None:
            self.handlers.disconnected(RoomDisconnectCause.CLIENT_INITIATED)

    # ------------------------------------------------------------- drivers --
    def _require(self) -> RoomHandlers:
        assert self.handlers is not None, "connect() first"
        return self.handlers

    def join(self, identity: str) -> None:
        self.present.add(identity)
        self._require().participant_joined(identity)

    def leave(self, identity: str) -> None:
        self.present.discard(identity)
        self._require().participant_left(identity)

    async def _mic_frames(self) -> AsyncIterator[bytes]:
        while (pcm := await self.mic.get()) is not None:
            yield pcm

    def open_microphone(self, identity: str) -> None:
        self._require().microphone_opened(identity, self._mic_frames())

    def speak(self, frames: int = 1, level: int = 1000) -> None:
        sample = level.to_bytes(2, "little", signed=True)
        for _ in range(frames):
            self.mic.put_nowait(sample * SAMPLES_48K_20MS)

    def end_microphone(self, identity: str) -> None:
        self.mic.put_nowait(None)
        self._require().microphone_closed(identity)

    def data(self, sender: str | None, topic: str | None, payload: bytes, reliable: bool) -> None:
        self._require().data_received(sender, topic, payload, reliable)

    def reconnecting(self) -> None:
        self._require().reconnecting()

    def reconnected(self) -> None:
        self._require().reconnected()

    def drop(self, cause: RoomDisconnectCause) -> None:
        self._require().disconnected(cause)
