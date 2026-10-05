"""Scripted, SDK-free fake of the Sarvam streaming seam (no network, no key).

Each ``flush`` releases the next per-segment script of the stream that
received it. Script steps are seam messages or the sentinels below:

- :data:`PAUSE`: block until the stream is closed (a provider that never
  answers; cancellation/timeouts must end it);
- :data:`END`: the iterator ends (connection lost without an error);
- a :class:`SarvamTransportError`: raised from the iterator.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    ErrorMessage,
    FinalMessage,
    SarvamErrorKind,
    SarvamMessage,
    SarvamTransportError,
    StreamSettings,
)

FRAME_BYTES: Final = 960
PAUSE: Final = "pause"
END: Final = "end"
Step = SarvamMessage | SarvamTransportError | str
Script = Sequence[Step]


def pcm(frames: float, level: int = 1000) -> bytes:
    """``frames`` 20 ms frames of a constant 16-bit sample (fractional -> partial frame)."""
    samples = int(frames * FRAME_BYTES / 2)
    return level.to_bytes(2, "little", signed=True) * samples


def audio(
    frames: float, request_id: str | None = "req-1", content_type: str = "audio/pcm"
) -> AudioMessage:
    return AudioMessage(pcm=pcm(frames), content_type=content_type, request_id=request_id)


def reply(*chunks: float) -> list[Step]:
    """Audio chunks (in frames) followed by the completion event."""
    return [*(audio(frames) for frames in chunks), FinalMessage()]


def error(code: int | None) -> ErrorMessage:
    return ErrorMessage(code=code, request_id="req-err")


def lost() -> SarvamTransportError:
    return SarvamTransportError(SarvamErrorKind.CONNECTION_LOST)


@dataclass
class FakeStream:
    scripts: deque[Script]
    configured: list[StreamSettings] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    flushes: int = 0
    pings: int = 0
    closed: bool = False
    fail_send: SarvamTransportError | None = None
    fail_ping: bool = False
    _queue: asyncio.Queue[Step] = field(default_factory=asyncio.Queue)
    _closed_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def configure(self, settings: StreamSettings) -> None:
        self._check()
        self.configured.append(settings)

    async def send_text(self, text: str) -> None:
        self._check()
        if self.fail_send is not None:
            raise self.fail_send
        self.texts.append(text)

    async def flush(self) -> None:
        self._check()
        self.flushes += 1
        script = self.scripts.popleft() if self.scripts else [FinalMessage()]
        for step in script:
            self._queue.put_nowait(step)

    async def ping(self) -> None:
        self._check()
        self.pings += 1
        if self.fail_ping:
            raise lost()

    def _check(self) -> None:
        if self.closed:
            raise lost()

    async def messages(self) -> AsyncIterator[SarvamMessage]:
        while True:
            step = await self._next()
            if step is None or step == END:
                return
            if step == PAUSE:
                await self._closed_event.wait()
                return
            if isinstance(step, SarvamTransportError):
                raise step
            if isinstance(step, str):  # pragma: no cover - unknown sentinel
                raise AssertionError(step)
            yield step

    async def _next(self) -> Step | None:
        getter = asyncio.ensure_future(self._queue.get())
        closer = asyncio.ensure_future(self._closed_event.wait())
        await asyncio.wait({getter, closer}, return_when=asyncio.FIRST_COMPLETED)
        closer.cancel()
        if getter.done():
            return getter.result()
        getter.cancel()
        return None

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


@dataclass
class FakeSarvamConnector:
    """``scripts`` are consumed in order across every stream the adapter opens."""

    scripts: deque[Script] = field(default_factory=deque)
    open_failures: deque[SarvamTransportError] = field(default_factory=deque)
    streams: list[FakeStream] = field(default_factory=list)
    opens: int = 0
    closed: bool = False

    @classmethod
    def with_scripts(cls, scripts: Iterable[Script]) -> FakeSarvamConnector:
        return cls(scripts=deque(scripts))

    async def open(self) -> FakeStream:
        self.opens += 1
        if self.open_failures:
            raise self.open_failures.popleft()
        stream = FakeStream(scripts=self.scripts)
        self.streams.append(stream)
        return stream

    async def aclose(self) -> None:
        self.closed = True

    @property
    def texts(self) -> list[str]:
        return [text for stream in self.streams for text in stream.texts]
