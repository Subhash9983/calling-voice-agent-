"""Sarvam Bulbul v3 streaming TTS over the official SDK WebSocket (docs/09; docs/13 §8).

Implements :class:`TTSPort`:

- one segment per ``synthesize`` call (one attempt, one operation ID):
  ``config`` (only when the language/settings changed), the bounded text,
  ``flush``, then audio chunks until the completion event;
- chunks are decoded inside the binding and re-framed here into normalized
  24 kHz mono 20 ms PCM frames (:class:`PcmFramer`);
- a 5 s first-audio deadline (``tts_first_audio_ms``), a stall deadline
  between chunks, and a total deadline; all non-retryable timeouts;
- ``cancel_segment``/``cancel_turn`` are observed between chunks; the
  connection is then **discarded**, because a clean post-cancel stream
  boundary cannot be proven (docs/09 §13). Only a segment that reached the
  completion event returns its connection for reuse (saves ~0.75 s connect),
  kept alive with pings while idle;
- usage is the accepted input character count (the billable quantity,
  docs/15 §2.5) plus generated audio duration: ``measured`` once the provider
  acknowledged the text with audio or completion, ``estimated`` when the text
  was sent but never acknowledged (cancel/failure may still be billed), and a
  measured zero when no text was sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from voice_agent.contracts.audio import RECOMMENDED_FRAME_MS, AudioFrame
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsFailed,
    TtsSegmentCompleted,
    TtsSegmentRequest,
    TtsVoiceConfig,
)
from voice_agent.contracts.usage import UsageReport, UsageSource
from voice_agent.costing.usage_normalization import tts_usage
from voice_agent.ports.clock import Clock
from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    ErrorMessage,
    FinalMessage,
    SarvamConnector,
    SarvamErrorKind,
    SarvamMessage,
    SarvamStream,
    SarvamTransportError,
    StreamSettings,
    code_kind,
)
from voice_agent.tts_adapters.sarvam.failures import tts_failure
from voice_agent.tts_adapters.sarvam.framing import PcmFramer
from voice_agent.tts_adapters.sarvam.options import (
    PCM_CONTENT_TYPES,
    check_voice,
    stream_settings,
)

DEFAULT_FIRST_AUDIO_MS: Final = 5000
DEFAULT_STALL_MS: Final = 5000
DEFAULT_TOTAL_MS: Final = 30_000
# The provider closes a connection after one idle minute; ping well before.
KEEPALIVE_INTERVAL_S: Final = 20.0
MS_PER_SECOND: Final = 1000
_LOGGER = logging.getLogger("voice_agent.tts_adapters.sarvam")


@dataclass(frozen=True, slots=True)
class SarvamTimeouts:
    first_audio_ms: int = DEFAULT_FIRST_AUDIO_MS
    stall_ms: int = DEFAULT_STALL_MS
    total_ms: int = DEFAULT_TOTAL_MS


class _Signal:
    def __init__(self, name: str) -> None:
        self.name = name


_CANCELLED: Final = _Signal("cancelled")
_TIMED_OUT: Final = _Signal("timed_out")
_ENDED: Final = _Signal("ended")


@dataclass(slots=True)
class _Live:
    stream: SarvamStream
    iterator: AsyncIterator[SarvamMessage]
    settings: StreamSettings | None = None
    keepalive: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Attempt:
    request: TtsSegmentRequest
    sent: bool = False
    acknowledged: bool = False
    clean: bool = False
    request_id: str | None = None
    frames: int = 0
    counters: dict[str, int] = field(default_factory=dict)


async def _next_message(
    iterator: AsyncIterator[SarvamMessage], cancel: asyncio.Event, timeout_s: float
) -> SarvamMessage | _Signal:
    if cancel.is_set():
        return _CANCELLED
    if timeout_s <= 0:
        return _TIMED_OUT
    pending = asyncio.ensure_future(anext(iterator))
    waiter = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait(
            {pending, waiter}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        waiter.cancel()
    if pending.done() and not cancel.is_set():
        try:
            return pending.result()
        except StopAsyncIteration:
            return _ENDED
    pending.cancel()
    with suppress(BaseException):
        await pending
    return _CANCELLED if cancel.is_set() else _TIMED_OUT


class SarvamTtsAdapter:
    def __init__(
        self,
        connector: SarvamConnector,
        *,
        clock: Clock,
        timeouts: SarvamTimeouts | None = None,
        keepalive_s: float = KEEPALIVE_INTERVAL_S,
        prewarm: bool = True,
    ) -> None:
        self._connector = connector
        self._clock = clock
        self._timeouts = timeouts or SarvamTimeouts()
        self._keepalive_s = keepalive_s
        self._prewarm = prewarm
        self._config: TtsVoiceConfig | None = None
        self._idle: _Live | None = None
        self._cancels: dict[str, asyncio.Event] = {}
        self._turn_segments: dict[str, set[str]] = {}
        self._playback_ms = 0
        self._background: set[asyncio.Task[None]] = set()
        self._closed = False
        self.counters: dict[str, int] = {}

    def __repr__(self) -> str:
        return "SarvamTtsAdapter()"

    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    # ------------------------------------------------------------ session --
    async def open_session(self, config: TtsVoiceConfig) -> None:
        check_voice(config)
        self._config = config
        if not self._prewarm or self._idle is not None:
            return
        try:
            self._park(await self._open())
        except SarvamTransportError:  # a cold connect is retried lazily per segment
            self._count("prewarm_failed")

    async def _open(self) -> _Live:
        stream = await self._connector.open()
        self._count("connections_opened")
        return _Live(stream=stream, iterator=aiter(stream.messages()))

    async def _acquire(self) -> _Live:
        live, self._idle = self._idle, None
        if live is None:
            return await self._open()
        if live.keepalive is not None:
            live.keepalive.cancel()
            live.keepalive = None
        self._count("connections_reused")
        return live

    def _park(self, live: _Live) -> None:
        if self._closed or self._idle is not None:
            task = asyncio.ensure_future(self._discard(live))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return
        live.keepalive = asyncio.ensure_future(self._keepalive(live))
        self._idle = live

    async def _keepalive(self, live: _Live) -> None:
        while True:
            await asyncio.sleep(self._keepalive_s)
            try:
                await live.stream.ping()
            except SarvamTransportError:
                if self._idle is live:
                    self._idle = None
                await self._discard(live)
                return

    async def _discard(self, live: _Live) -> None:
        if live.keepalive is not None:
            live.keepalive.cancel()
        self._count("connections_discarded")
        with suppress(Exception):
            await live.stream.close()

    # --------------------------------------------------------- synthesize --
    async def synthesize(self, request: TtsSegmentRequest) -> AsyncIterator[TtsEvent]:
        config = self._config
        if config is None or self._closed:
            raise RuntimeError("TTS session is not open")
        self._cancels.setdefault(request.segment_id, asyncio.Event())
        turn_id = request.stamp.turn_id or ""
        self._turn_segments.setdefault(turn_id, set()).add(request.segment_id)
        attempt = _Attempt(request)
        try:
            events = self._attempt_events(attempt, stream_settings(config, request.language_code))
            try:
                async for event in events:
                    yield event
            finally:
                await events.aclose()
        finally:
            self._cancels.pop(request.segment_id, None)
            self._turn_segments.get(turn_id, set()).discard(request.segment_id)

    async def _attempt_events(
        self, attempt: _Attempt, settings: StreamSettings
    ) -> AsyncGenerator[TtsEvent]:
        cancel = self._cancels[attempt.request.segment_id]
        try:
            live = await self._acquire()
        except SarvamTransportError as error:
            yield self._failed(attempt, error.kind, phase="connect", status_code=error.status_code)
            return
        try:
            async for event in self._stream(attempt, live, settings, cancel):
                yield event
        finally:
            if attempt.clean:
                self._park(live)
            else:
                await self._discard(live)

    async def _stream(
        self, attempt: _Attempt, live: _Live, settings: StreamSettings, cancel: asyncio.Event
    ) -> AsyncGenerator[TtsEvent]:
        if cancel.is_set():
            yield self._cancelled(attempt)
            return
        try:
            if live.settings != settings:
                await live.stream.configure(settings)
                live.settings = settings
            await live.stream.send_text(attempt.request.text)
            attempt.sent = True
            await live.stream.flush()
        except SarvamTransportError as error:
            yield self._failed(attempt, error.kind, phase="send", status_code=error.status_code)
            return
        async for event in self._receive(attempt, live, cancel):
            yield event

    def _limit_s(self, attempt: _Attempt, started: float, last: float, now: float) -> float:
        timeouts = self._timeouts
        total_left = timeouts.total_ms / MS_PER_SECOND - (now - started)
        if not attempt.acknowledged:
            first_left = timeouts.first_audio_ms / MS_PER_SECOND - (now - started)
            return min(first_left, total_left)
        return min(timeouts.stall_ms / MS_PER_SECOND - (now - last), total_left)

    async def _receive(
        self, attempt: _Attempt, live: _Live, cancel: asyncio.Event
    ) -> AsyncGenerator[TtsEvent]:
        loop = asyncio.get_running_loop()
        started = last = loop.time()
        framer = PcmFramer(session_id=attempt.request.stamp.session_id, start_ms=self._playback_ms)
        try:
            while True:
                limit = self._limit_s(attempt, started, last, loop.time())
                try:
                    step = await _next_message(live.iterator, cancel, limit)
                except SarvamTransportError as error:
                    phase = "stream" if attempt.acknowledged else "first_audio"
                    yield self._failed(
                        attempt, error.kind, phase=phase, status_code=error.status_code
                    )
                    return
                last = loop.time()
                if isinstance(step, FinalMessage):
                    for frame_event in self._chunks(attempt, framer.flush()):
                        yield frame_event
                terminal = self._terminal_for(attempt, step)
                if terminal is not None:
                    yield terminal
                    return
                if isinstance(step, AudioMessage):
                    for frame_event in self._audio(attempt, framer, step):
                        yield frame_event
                        if isinstance(frame_event, TtsFailed):
                            return
        finally:
            self._playback_ms = framer.next_ms

    def _terminal_for(self, attempt: _Attempt, step: SarvamMessage | _Signal) -> TtsEvent | None:
        if step is _CANCELLED:
            return self._cancelled(attempt)
        if step is _TIMED_OUT:
            kind = (
                SarvamErrorKind.TOTAL_TIMEOUT
                if attempt.acknowledged
                else SarvamErrorKind.FIRST_AUDIO_TIMEOUT
            )
            return self._failed(attempt, kind, phase=kind.value)
        if step is _ENDED:
            return self._failed(attempt, SarvamErrorKind.CONNECTION_LOST, phase="stream_ended")
        if isinstance(step, ErrorMessage):
            attempt.request_id = attempt.request_id or step.request_id
            return self._failed(
                attempt, code_kind(step.code), phase="provider", status_code=step.code
            )
        if isinstance(step, FinalMessage):
            attempt.acknowledged = True
            attempt.clean = True
            return TtsSegmentCompleted(
                stamp=attempt.request.stamp,
                segment_id=attempt.request.segment_id,
                usage=self._usage(attempt),
                provider_request_id=attempt.request_id,
            )
        return None

    def _audio(self, attempt: _Attempt, framer: PcmFramer, message: AudioMessage) -> list[TtsEvent]:
        if message.content_type.split(";")[0].strip().lower() not in PCM_CONTENT_TYPES:
            return [self._failed(attempt, SarvamErrorKind.CORRUPT_AUDIO, phase="decode")]
        attempt.acknowledged = True
        attempt.request_id = attempt.request_id or message.request_id
        try:
            frames = framer.push(message.pcm)
        except SarvamTransportError as error:
            return [self._failed(attempt, error.kind, phase="decode")]
        return self._chunks(attempt, frames)

    def _chunks(self, attempt: _Attempt, frames: list[AudioFrame]) -> list[TtsEvent]:
        request = attempt.request
        attempt.frames += len(frames)
        return [
            TtsAudioChunk(stamp=request.stamp, segment_id=request.segment_id, frame=frame)
            for frame in frames
        ]

    # ------------------------------------------------------------ results --
    def _usage(self, attempt: _Attempt) -> UsageReport:
        audio_ms = attempt.frames * RECOMMENDED_FRAME_MS
        if not attempt.sent:
            return tts_usage(
                synthesized_characters=0, generated_audio_ms=0, source=UsageSource.MEASURED
            )
        source = UsageSource.MEASURED if attempt.acknowledged else UsageSource.ESTIMATED
        return tts_usage(
            synthesized_characters=len(attempt.request.text),
            generated_audio_ms=audio_ms,
            source=source,
        )

    def _cancelled(self, attempt: _Attempt) -> TtsCancelled:
        return TtsCancelled(
            stamp=attempt.request.stamp,
            segment_id=attempt.request.segment_id,
            usage=self._usage(attempt),
        )

    def _failed(
        self,
        attempt: _Attempt,
        kind: SarvamErrorKind,
        *,
        phase: str,
        status_code: int | None = None,
    ) -> TtsFailed:
        attempt.clean = False
        _LOGGER.warning(
            "tts.sarvam_failed", extra={"safe_fields": {"kind": kind.value, "phase": phase}}
        )
        failure = tts_failure(
            kind,
            stamp=attempt.request.stamp,
            occurred_at=self._clock.utc_now(),
            phase=phase,
            status_code=status_code,
        )
        return TtsFailed(
            stamp=attempt.request.stamp,
            segment_id=attempt.request.segment_id,
            failure=failure,
            usage=self._usage(attempt),
        )

    # ------------------------------------------------------------- cancel --
    async def cancel_segment(self, segment_id: str) -> None:
        """Observed between chunks; a finished segment has nothing left to cancel."""
        event = self._cancels.get(segment_id)
        if event is not None:
            event.set()

    async def cancel_turn(self, turn_id: str) -> None:
        for segment_id in tuple(self._turn_segments.get(turn_id, ())):
            await self.cancel_segment(segment_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for event in self._cancels.values():
            event.set()
        idle, self._idle = self._idle, None
        if idle is not None:
            await self._discard(idle)
        await self._connector.aclose()
