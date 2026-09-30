"""Transcript assembly and turn finalization for the Deepgram stream (docs/07 §8-§11, §13).

- interim results become lossy partials (stable segments + current interim);
- ``is_final`` results are mapped to the capture timeline and stored in the
  bounded segment assembler; ``speech_final`` is an advisory counter only;
- a requested finalization is resolved by the next ``from_finalize`` result
  of the same stream (FIFO), or by the 3 s timeout with what was assembled;
  a late ``from_finalize`` after the timeout is counted and discarded;
- a turn whose window overlaps audio lost with a failed stream fails instead
  of receiving an invented or partial transcript;
- cancelled turns never produce results.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final

from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    MAX_FINAL_TRANSCRIPT_CHARS,
    AudioWindow,
    SttFailed,
    SttFinalSegment,
    SttPartial,
    SttTurnFinalized,
)
from voice_agent.ports.clock import IdGenerator
from voice_agent.stt_adapters.deepgram.assembly import MappedWord, Segment, SegmentAssembler
from voice_agent.stt_adapters.deepgram.connection import DeepgramErrorKind
from voice_agent.stt_adapters.deepgram.messages import ResultsMessage
from voice_agent.stt_adapters.deepgram.sink import EventSink
from voice_agent.stt_adapters.deepgram.stream import PendingFinalize, StreamAttempt

MAX_LOST_INTERVALS: Final = 20
MAX_CANCELLED_TURNS: Final = 100

FailureFactory = Callable[..., NormalizedFailure]


class TurnFinalizer:
    def __init__(
        self,
        sink: EventSink,
        *,
        ids: IdGenerator,
        failure: FailureFactory,
        finalize_timeout_ms: int,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._sink = sink
        self._ids = ids
        self._failure = failure
        self._timeout_s = finalize_timeout_ms / 1000
        self._sleep = sleep
        self._assembler = SegmentAssembler()
        self._pending: list[PendingFinalize] = []
        self._cancelled: deque[str] = deque(maxlen=MAX_CANCELLED_TURNS)
        self._lost: deque[tuple[int, int]] = deque(maxlen=MAX_LOST_INTERVALS)
        self._revision = 0
        self._last_partial = ""
        self.partials_enabled = True

    # ------------------------------------------------------------ results --
    async def on_results(self, attempt: StreamAttempt, message: ResultsMessage) -> None:
        if message.speech_final:
            self._sink.count("provider_speech_final")  # advisory only (docs/07 §10)
        if message.is_final:
            attempt.last_final_capture_end_ms = attempt.times.to_capture_ms(message.end_s)
            if not self._assembler.add(_segment(attempt, message)):
                self._sink.count("duplicate_finals")
            self._partial("", message)
        else:
            self._partial(message.transcript, message)
        if message.from_finalize:
            await self._acknowledge(attempt)

    def _partial(self, interim: str, message: ResultsMessage) -> None:
        if not self.partials_enabled:
            return
        text = " ".join(t for t in (self._assembler.pending_text(), interim.strip()) if t)
        if not text or text == self._last_partial:
            return
        self._last_partial = text
        self._revision += 1
        self._sink.emit_lossy(
            SttPartial(
                stamp=self._sink.stream_stamp(),
                revision=self._revision,
                text=text[-MAX_FINAL_TRANSCRIPT_CHARS:],
                language=message.languages[0] if message.languages else None,
                confidence=message.confidence,
            )
        )

    async def _acknowledge(self, attempt: StreamAttempt) -> None:
        if not attempt.tokens:
            self._sink.count("unexpected_finalize_results")
            return
        pending = attempt.tokens.popleft()
        if pending.resolved:
            self._sink.count("late_finalize_results")
            return
        await self.resolve(pending, timed_out=False)

    # ----------------------------------------------------------- requests --
    def is_cancelled(self, turn_id: str) -> bool:
        return turn_id in self._cancelled

    def overlaps_lost(self, window: AudioWindow | None) -> bool:
        if window is None:
            return bool(self._lost)
        return any(start < window.end_ms and end > window.start_ms for start, end in self._lost)

    def record_lost(self, interval: tuple[int, int]) -> None:
        self._lost.append(interval)

    def track(self, stamp: GenerationStamp, window: AudioWindow | None) -> PendingFinalize:
        pending = PendingFinalize(stamp=stamp, window=window)
        self._pending.append(pending)
        pending.timer = asyncio.create_task(self._deadline(pending))
        return pending

    def unsent(self) -> list[PendingFinalize]:
        return [pending for pending in self._pending if not pending.sent]

    async def _deadline(self, pending: PendingFinalize) -> None:
        await self._sleep(self._timeout_s)
        if not pending.resolved:
            await self._sink.warn("finalization_timeout")
            await self.resolve(pending, timed_out=True)

    def settle(self, pending: PendingFinalize) -> bool:
        """Mark resolved; ``True`` when the turn may still receive a result."""
        if pending.resolved:
            return False
        pending.resolved = True
        if pending.timer is not None and pending.timer is not asyncio.current_task():
            pending.timer.cancel()
        if pending in self._pending:
            self._pending.remove(pending)
        return pending.stamp.turn_id not in self._cancelled

    def cancel(self, turn_id: str) -> None:
        self._cancelled.append(turn_id)
        for pending in list(self._pending):
            if pending.stamp.turn_id == turn_id:
                self.settle(pending)

    def abandon_all(self) -> None:
        for pending in list(self._pending):
            self.settle(pending)

    # ---------------------------------------------------------- resolution --
    async def resolve(self, pending: PendingFinalize, *, timed_out: bool) -> None:
        if not self.settle(pending):
            return
        operation_id = self._sink.stream_stamp().operation_id
        stamp = pending.stamp if operation_id is None else pending.stamp.for_operation(operation_id)
        taken = self._assembler.take(pending.window)
        self._last_partial = ""
        for segment in taken.segments:
            await self._sink.emit(
                SttFinalSegment(
                    stamp=stamp,
                    text=segment.text,
                    audio_start_ms=segment.start_ms,
                    audio_end_ms=max(segment.end_ms, segment.start_ms),
                    language=segment.languages[0] if segment.languages else None,
                    confidence=segment.confidence,
                )
            )
        await self._sink.emit(
            SttTurnFinalized(
                stamp=stamp,
                transcript_id=self._ids.new_id(),
                text=taken.text[:MAX_FINAL_TRANSCRIPT_CHARS],
                language=taken.language,
                confidence=taken.confidence,
                finalization_timed_out=timed_out,
            )
        )

    async def fail_turn(self, pending: PendingFinalize, kind: DeepgramErrorKind) -> None:
        if not self.settle(pending):
            return
        failure = self._failure(
            kind,
            "turn_audio_unconfirmed",
            turn_id=pending.stamp.turn_id,
            operation_id=self._sink.stream_stamp().operation_id,
            user_affected=True,
        )
        await self._sink.emit(SttFailed(stamp=pending.stamp, failure=failure))

    async def settle_after_loss(self, attempt: StreamAttempt) -> None:
        """Turns waiting on a lost stream: fail if their audio is unconfirmed, else resolve."""
        for pending in list(attempt.tokens):
            if pending.resolved:
                continue
            if self.overlaps_lost(pending.window):
                await self.fail_turn(pending, DeepgramErrorKind.CONNECTION_LOST)
            else:
                await self.resolve(pending, timed_out=False)

    async def fail_all(self, kind: DeepgramErrorKind) -> None:
        for pending in list(self._pending):
            await self.fail_turn(pending, kind)


def _segment(attempt: StreamAttempt, message: ResultsMessage) -> Segment:
    to_ms = attempt.times.to_capture_ms
    return Segment(
        start_ms=to_ms(message.start_s),
        end_ms=to_ms(message.end_s),
        text=message.transcript.strip(),
        words=tuple(
            MappedWord(w.text, to_ms(w.start_s), to_ms(w.end_s), w.language) for w in message.words
        ),
        languages=message.languages,
        confidence=message.confidence,
    )
