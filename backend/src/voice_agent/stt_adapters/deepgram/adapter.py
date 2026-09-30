"""Deepgram Nova-3 multilingual session-stream STT adapter (docs/07 §3, §7-§14).

One live stream carries every turn of the session. Behaviour:

- ``start`` maps the normalized config (Nova-3, ``language=multi``, 16 kHz
  linear16, interim results, punctuation/smart formatting, keyterms only if
  approved) and connects with bounded timeout and allowlisted retry; each
  attempt is its own operation (``stt.stream_started`` / ``stt.stream_closed``);
- ``write_audio`` streams 20 ms frames unchanged and records the stream
  offset -> capture-time map; while a recovery runs, frames wait in a bounded
  inbound buffer and are replayed (never sent twice);
- transcript assembly and turn finalization live in :mod:`turns`;
  ``SpeechStarted``/``UtteranceEnd``/``speech_final`` are advisory counters;
- a silent provider (no message while audio flows) or a dropped socket is a
  lost attempt: a new attempt is opened, and audio sent without a finalized
  result fails the overlapping turn rather than inventing a transcript;
- ``close`` sends ``CloseStream``, waits (bounded) for ``Metadata`` usage, and
  ends the event stream idempotently.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from enum import StrEnum

from voice_agent.contracts.audio import VAD_SAMPLE_RATE_HZ, AudioFrame
from voice_agent.contracts.failures import NormalizedFailure, NormalizedFailureError
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.stt import (
    AudioWindow,
    SttAttempt,
    SttEvent,
    SttFailed,
    SttStreamConfig,
    SttStreamOutcome,
    SttStreamStarted,
    SttUsage,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.stt_adapters.deepgram.connection import (
    DeepgramConnector,
    DeepgramErrorKind,
    DeepgramTransportError,
)
from voice_agent.stt_adapters.deepgram.failures import stt_failure
from voice_agent.stt_adapters.deepgram.messages import (
    DeepgramMessage,
    MalformedMessage,
    MetadataMessage,
    ResultsMessage,
    SpeechStartedMessage,
    UnknownMessage,
    UtteranceEndMessage,
    parse_message,
)
from voice_agent.stt_adapters.deepgram.options import (
    DEEPGRAM_MODEL,
    DEEPGRAM_PROVIDER,
    DeepgramStreamOptions,
    UnsupportedSttConfigError,
    build_stream_options,
)
from voice_agent.stt_adapters.deepgram.sink import EventSink
from voice_agent.stt_adapters.deepgram.stream import PendingFinalize, StreamAttempt
from voice_agent.stt_adapters.deepgram.turns import TurnFinalizer


@dataclass(frozen=True, slots=True)
class DeepgramTiming:
    connect_timeout_s: float = 5.0
    close_timeout_s: float = 3.0
    finalize_timeout_ms: int = 3000
    maintenance_interval_s: float = 1.0
    keepalive_idle_ms: int = 4000
    response_timeout_ms: int = 15_000
    inbound_buffer_ms: int = 2000
    warning_cap: int = 5
    event_buffer: int = 256


class _State(StrEnum):
    IDLE = "idle"
    OPEN = "open"
    RECOVERING = "recovering"
    FAILED = "failed"
    CLOSED = "closed"


def default_retry_delay_ms(failed_attempt: int) -> int:
    return min(250 * (1 << max(failed_attempt - 1, 0)), 2000)


class DeepgramSttAdapter:
    def __init__(
        self,
        connector: DeepgramConnector,
        *,
        session_id: str,
        worker_generation: int,
        clock: Clock,
        ids: IdGenerator,
        model: str = DEEPGRAM_MODEL,
        retry: RetryPolicy | None = None,
        retry_delay_ms: Callable[[int], int] = default_retry_delay_ms,
        keyterms_approved: bool = False,
        timing: DeepgramTiming | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._connector = connector
        self._clock = clock
        self._ids = ids
        self._model = model
        self._retry = retry or RetryPolicy()
        self._retry_delay_ms = retry_delay_ms
        self._keyterms_approved = keyterms_approved
        self._timing = timing or DeepgramTiming()
        self._sleep = sleep
        self._base = GenerationStamp(
            session_id=session_id, worker_generation=worker_generation, cancellation_generation=0
        )
        self._state = _State.IDLE
        self._options: DeepgramStreamOptions | None = None
        self._logical_request_id = ""
        self._attempt_number = 0
        self._attempt: StreamAttempt | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._inbound: deque[AudioFrame] = deque()
        self._background: set[asyncio.Task[None]] = set()
        self._sink = EventSink(
            base=self._base,
            capacity=self._timing.event_buffer,
            warning_cap=self._timing.warning_cap,
            current=lambda: self._attempt,
        )
        self._turns = TurnFinalizer(
            self._sink,
            ids=ids,
            failure=self._failure,
            finalize_timeout_ms=self._timing.finalize_timeout_ms,
            sleep=sleep,
        )

    @property
    def counters(self) -> dict[str, int]:
        return self._sink.counters

    def _now(self) -> int:
        return self._clock.monotonic_ms()

    def _failure(self, kind: DeepgramErrorKind, phase: str, **fields: object) -> NormalizedFailure:
        return stt_failure(
            kind,
            session_id=self._base.session_id,
            occurred_at=self._clock.utc_now(),
            phase=phase,
            **fields,  # type: ignore[arg-type]
        )

    # --------------------------------------------------------------- start --
    async def start(self, config: SttStreamConfig) -> None:
        if self._state is not _State.IDLE:
            raise RuntimeError("the STT stream was already started")
        try:
            self._options = build_stream_options(
                config, model=self._model, keyterms_approved=self._keyterms_approved
            )
        except UnsupportedSttConfigError as rejected:
            failure = self._failure(DeepgramErrorKind.CONFIGURATION, rejected.reason)
            self._state = _State.FAILED
            await self._sink.emit(SttFailed(stamp=self._base, failure=failure))
            raise NormalizedFailureError(failure) from None
        self._turns.partials_enabled = config.partial_transcripts
        self._logical_request_id = self._ids.new_id()
        await self._open_with_retry(previous=None)
        self._state = _State.OPEN

    def _new_attempt(self, previous: str | None) -> StreamAttempt:
        self._attempt_number += 1
        operation_id = self._ids.new_id()
        identity = SttAttempt(
            logical_request_id=self._logical_request_id,
            attempt_number=self._attempt_number,
            previous_attempt_operation_id=previous,
            provider=DEEPGRAM_PROVIDER,
            model=self._model,
        )
        return StreamAttempt(
            operation_id=operation_id,
            identity=identity,
            stamp=self._base.for_operation(operation_id),
        )

    async def _open_with_retry(self, previous: str | None) -> None:
        for budget in range(1, self._retry.maximum_attempts + 1):
            attempt = self._new_attempt(previous)
            failure = await self._connect(attempt)
            if failure is None:
                return
            closed = attempt.closed_event(SttStreamOutcome.FAILED, self._now(), failure)
            await self._sink.emit(closed)
            if not self._retryable(failure) or budget == self._retry.maximum_attempts:
                raise NormalizedFailureError(failure)
            await self._sleep(self._retry_delay_ms(budget) / 1000)
            previous = attempt.operation_id

    def _retryable(self, failure: NormalizedFailure) -> bool:
        return failure.retryable and failure.error_type in self._retry.retryable_error_types

    async def _connect(self, attempt: StreamAttempt) -> NormalizedFailure | None:
        options = self._options
        if options is None:
            raise RuntimeError("stream options are built before connecting")
        began = self._now()
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self._timing.connect_timeout_s):
                connection = await stack.enter_async_context(
                    self._connector.connect(options.params)
                )
        except DeepgramTransportError as error:
            await stack.aclose()
            return self._failure(
                error.kind,
                "connect",
                status_code=error.status_code,
                operation_id=attempt.operation_id,
            )
        except TimeoutError:
            await stack.aclose()
            return self._failure(
                DeepgramErrorKind.CONNECT_FAILED,
                "connect_timeout",
                operation_id=attempt.operation_id,
            )
        now = self._now()
        attempt.connection, attempt.stack = connection, stack
        attempt.connect_ms, attempt.connected_at_ms = now - began, now
        attempt.last_sent_at_ms = attempt.last_message_at_ms = attempt.last_keepalive_at_ms = now
        self._attempt = attempt
        attempt.tasks.append(asyncio.create_task(self._read(attempt)))
        attempt.tasks.append(asyncio.create_task(self._maintain(attempt)))
        await self._sink.emit(
            SttStreamStarted(
                stamp=attempt.stamp,
                attempt=attempt.identity,
                connect_ms=attempt.connect_ms,
                keyterm_count=options.keyterm_count,
            )
        )
        return None

    # --------------------------------------------------------------- audio --
    async def write_audio(self, frame: AudioFrame, stamp: GenerationStamp) -> None:
        if self._state in (_State.IDLE, _State.CLOSED):
            raise RuntimeError("STT stream is not open")
        if stamp.session_id != frame.session_id or frame.session_id != self._base.session_id:
            raise ValueError("frame and stamp belong to different sessions")
        if frame.sample_rate_hz != VAD_SAMPLE_RATE_HZ:
            raise ValueError("the Deepgram stream accepts 16 kHz linear16 frames")
        if self._state is _State.FAILED:
            self._sink.count("audio_dropped_stream_failed_ms", frame.duration_ms)
            return
        if self._state is _State.RECOVERING:
            await self._buffer(frame)
            return
        await self._send(frame)

    async def _send(self, frame: AudioFrame) -> None:
        attempt = self._attempt
        if attempt is None:
            raise RuntimeError("an open stream always has a current attempt")
        try:
            await attempt.send_audio(frame, self._now())
        except DeepgramTransportError as error:
            self._inbound.appendleft(frame)
            await self._lose(attempt, error.kind, error.status_code)

    async def _buffer(self, frame: AudioFrame) -> None:
        self._inbound.append(frame)
        buffered = sum(f.duration_ms for f in self._inbound)
        while buffered > self._timing.inbound_buffer_ms:
            dropped = self._inbound.popleft()
            buffered -= dropped.duration_ms
            self._turns.record_lost((dropped.captured_at_ms, dropped.ends_at_ms))
            self._sink.count("audio_overflow_ms", dropped.duration_ms)
            await self._sink.warn("inbound_audio_overflow")

    # ------------------------------------------------------------- reading --
    async def _read(self, attempt: StreamAttempt) -> None:
        connection = attempt.connection
        if connection is None:
            return
        try:
            async for raw in connection.messages():
                await self._on_message(attempt, parse_message(raw))
        except DeepgramTransportError as error:
            await self._lose(attempt, error.kind, error.status_code)
            return
        except Exception:  # an unexpected SDK failure is normalized, never propagated
            attempt.counters["reader_errors"] += 1
            await self._lose(attempt, DeepgramErrorKind.CONNECTION_LOST, None)
            return
        attempt.finished = True
        if not attempt.closing:
            await self._lose(attempt, DeepgramErrorKind.CONNECTION_LOST, None)

    async def _on_message(self, attempt: StreamAttempt, message: DeepgramMessage) -> None:
        now = self._now()
        attempt.last_message_at_ms = now
        attempt.awaiting_response = False
        if attempt is not self._attempt:
            attempt.counters["late_results"] += 1
            return
        if isinstance(message, ResultsMessage):
            if attempt.first_result_at_ms is None:
                attempt.first_result_at_ms = now
            attempt.request_id = attempt.request_id or message.request_id
            await self._turns.on_results(attempt, message)
        elif isinstance(message, MetadataMessage):
            attempt.provider_duration_s = message.duration_s
            attempt.request_id = message.request_id or attempt.request_id
        elif isinstance(message, SpeechStartedMessage):
            self._sink.count("provider_speech_started")  # advisory only (docs/07 §10)
        elif isinstance(message, UtteranceEndMessage):
            self._sink.count("provider_utterance_end")  # advisory only
        elif isinstance(message, UnknownMessage):
            self._sink.count("unknown_messages")
        elif isinstance(message, MalformedMessage):
            self._sink.count("malformed_messages")
            await self._sink.warn("protocol_invalid_message")

    # -------------------------------------------------------- finalization --
    async def finalize_turn(
        self, stamp: GenerationStamp, window: AudioWindow | None = None
    ) -> None:
        if stamp.turn_id is None:
            raise ValueError("finalization requires a turn")
        if self._state in (_State.IDLE, _State.CLOSED):
            raise RuntimeError("STT stream is not open")
        if self._turns.is_cancelled(stamp.turn_id):
            return
        if self._turns.overlaps_lost(window) or self._state is _State.FAILED:
            await self._turns.fail_turn(
                PendingFinalize(stamp=stamp, window=window), DeepgramErrorKind.CONNECTION_LOST
            )
            return
        pending = self._turns.track(stamp, window)
        if self._state is _State.OPEN:
            await self._send_finalize(pending)

    async def _send_finalize(self, pending: PendingFinalize) -> None:
        attempt = self._attempt
        if attempt is None or attempt.connection is None or pending.resolved:
            return
        pending.sent = True
        attempt.tokens.append(pending)
        try:
            await attempt.connection.send_finalize()
        except DeepgramTransportError as error:
            await self._lose(attempt, error.kind, error.status_code)

    async def cancel_turn(self, turn_id: str) -> None:
        self._turns.cancel(turn_id)

    # ------------------------------------------------------------ recovery --
    async def _maintain(self, attempt: StreamAttempt) -> None:
        timing = self._timing
        while attempt is self._attempt and not attempt.closing:
            await self._sleep(timing.maintenance_interval_s)
            now = self._now()
            if (
                attempt.awaiting_response
                and now - attempt.last_message_at_ms >= timing.response_timeout_ms
            ):
                self._sink.count("response_timeouts")
                await self._lose(attempt, DeepgramErrorKind.CONNECTION_LOST, None)
                return
            idle = now - max(attempt.last_sent_at_ms, attempt.last_keepalive_at_ms)
            if idle >= timing.keepalive_idle_ms and attempt.connection is not None:
                attempt.last_keepalive_at_ms = now
                self._sink.count("keepalives")
                try:
                    await attempt.connection.send_keep_alive()
                except DeepgramTransportError as error:
                    await self._lose(attempt, error.kind, error.status_code)
                    return

    async def _lose(
        self, attempt: StreamAttempt, kind: DeepgramErrorKind, status_code: int | None
    ) -> None:
        if attempt is not self._attempt or self._state is not _State.OPEN or attempt.closing:
            return
        self._state = _State.RECOVERING
        interval = attempt.unconfirmed_interval()
        if interval is not None:
            self._turns.record_lost(interval)
        failure = self._failure(
            kind, "stream", status_code=status_code, operation_id=attempt.operation_id
        )
        await self._sink.emit(attempt.closed_event(SttStreamOutcome.FAILED, self._now(), failure))
        await self._turns.settle_after_loss(attempt)
        attempt.closing = True
        self._spawn(attempt.aclose(self._timing.close_timeout_s))
        self._recovery = asyncio.create_task(self._recover(attempt.operation_id))

    def _spawn(self, awaitable: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _recover(self, previous: str) -> None:
        try:
            await self._open_with_retry(previous)
        except NormalizedFailureError as exhausted:
            await self._give_up(exhausted.failure)
            return
        if self._state is _State.CLOSED:
            return
        self._state = _State.OPEN
        self._sink.count("recoveries")
        while self._inbound and self._state is _State.OPEN:
            await self._send(self._inbound.popleft())
        for pending in self._turns.unsent():
            await self._send_finalize(pending)

    async def _give_up(self, failure: NormalizedFailure) -> None:
        self._state = _State.FAILED
        self._attempt = None
        dropped = sum(frame.duration_ms for frame in self._inbound)
        self._inbound.clear()
        if dropped:
            self._sink.count("audio_dropped_stream_failed_ms", dropped)
        await self._sink.emit(SttFailed(stamp=self._base, failure=failure))
        await self._turns.fail_all(DeepgramErrorKind.UNAVAILABLE)

    # --------------------------------------------------------------- close --
    def events(self) -> AsyncIterator[SttEvent]:
        return self._sink.drain()

    async def close(self) -> None:
        if self._state is _State.CLOSED:
            return
        self._state = _State.CLOSED
        if self._recovery is not None:
            self._recovery.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery
        self._turns.abandon_all()
        attempt = self._attempt
        if attempt is not None and not attempt.closing:
            await self._close_attempt(attempt)
        self._sink.force_put(None)

    async def _close_attempt(self, attempt: StreamAttempt) -> None:
        attempt.closing = True
        reader = attempt.tasks[0] if attempt.tasks else None
        try:
            async with asyncio.timeout(self._timing.close_timeout_s):
                if attempt.connection is not None:
                    await attempt.connection.send_close_stream()
                if reader is not None:
                    await asyncio.shield(reader)
        except (TimeoutError, DeepgramTransportError):
            self._sink.count("close_without_metadata")
        now = self._now()
        await attempt.aclose(self._timing.close_timeout_s)
        self._sink.force_put(SttUsage(stamp=attempt.stamp, usage=attempt.usage(now)))
        self._sink.force_put(attempt.closed_event(SttStreamOutcome.SUCCEEDED, now))
