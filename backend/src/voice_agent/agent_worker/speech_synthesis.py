"""Synthesis stage of the speech pipeline: one TTS piece, fenced, retried, evidenced (WP9).

For each piece (docs/09 §12-§14, §18; docs/05 §14-§17):

- every attempt is one ``provider_operations`` row (``tts`` /
  ``synthesize_stream``) with its own operation ID, registered with the
  conversation gate's generation fence for the attempt's lifetime;
- every adapter event is fence-checked; the first stale verdict stops
  forwarding for good and cancels the provider segment; later frames are only
  counted (late audio can never be queued for playback);
- only allowlisted transient failures retry, and only while nothing of the
  piece was forwarded to playback (no replay of delivered audio); each retry
  is a new attempt with separate timing/usage/cost evidence;
- an outer guard bounds an adapter that never terminates;
- evidence: first provider audio ms (``time_to_first_result_ms``, which the
  control API's ``/operations`` reports), total synthesis ms, accepted input
  characters (the billable quantity) and generated audio, original vs
  normalized character counts, and the safe provider request ID. Writes go
  through the ordered writer, never on the audio path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from pydantic import JsonValue

from voice_agent.agent_worker.ordered_writer import OrderedWriter
from voice_agent.agent_worker.speech_tracks import EndItem, FrameItem, PlaybackItem, SegmentTrack
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.audio import RECOMMENDED_FRAME_MS
from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsFailed,
    TtsSegmentCompleted,
    TtsSegmentRequest,
)
from voice_agent.contracts.usage import UsageReport
from voice_agent.domain.operation import ProviderOperation
from voice_agent.orchestration.generations import FenceVerdict, GenerationFence
from voice_agent.orchestration.retry import RetryContext, decide_retry
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.tts import TTSPort

SYNTHESIZE_OPERATION: Final = "synthesize_stream"
MS_PER_SECOND: Final = 1000
DEFAULT_ATTEMPT_GUARD_MS: Final = 40_000


@dataclass(frozen=True, slots=True)
class SpeechSetup:
    session_id: str
    worker_generation: int
    provider: str
    model: str
    voice_id: str
    voice_configuration_version: int = 1
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    attempt_guard_ms: int = DEFAULT_ATTEMPT_GUARD_MS
    max_queued_segments: int = 5
    max_buffered_frames: int = 3000
    ack_grace_ms: int = 500


@dataclass(slots=True)
class _Result:
    terminal: TtsEvent | None = None
    first_audio_ms: int | None = None
    total_ms: int = 0
    frames: int = 0
    stale: bool = False

    @property
    def failure(self) -> NormalizedFailure | None:
        return self.terminal.failure if isinstance(self.terminal, TtsFailed) else None

    @property
    def usage(self) -> UsageReport:
        terminal = self.terminal
        if isinstance(terminal, TtsSegmentCompleted | TtsCancelled | TtsFailed):
            return terminal.usage
        return UsageReport.unavailable()


class SegmentSynthesizer:
    def __init__(
        self,
        setup: SpeechSetup,
        *,
        tts: TTSPort,
        fence: GenerationFence,
        evidence: SttEvidence,
        writer: OrderedWriter,
        clock: Clock,
        ids: IdGenerator,
        put: Callable[[PlaybackItem], Awaitable[None]],
        jitter: Callable[[], float],
    ) -> None:
        self._setup = setup
        self._tts = tts
        self._fence = fence
        self._evidence = evidence
        self._writer = writer
        self._clock = clock
        self._ids = ids
        self._put = put
        self._jitter = jitter

    async def synthesize(self, track: SegmentTrack) -> None:
        """Synthesize one piece; sets ``track.failure`` when it finally failed."""
        turn_stamp = track.stamp
        operation = self._operation(turn_stamp)
        while True:
            result = await self._attempt(track, operation)
            failure = result.failure
            if failure is None or result.stale:
                return
            context = RetryContext(
                attempt_number=operation.attempt_number,
                output_delivered=track.frames_forwarded > 0,
            )
            decision = decide_retry(self._setup.retry, failure, context, jitter=self._jitter())
            if not decision.should_retry or not self._current(turn_stamp):
                track.failure = failure
                return
            await asyncio.sleep((decision.backoff_ms or 0) / MS_PER_SECOND)
            if not self._current(turn_stamp):
                return
            operation = operation.next_attempt(self._ids.new_id())

    def _current(self, stamp: GenerationStamp) -> bool:
        return self._fence.check(stamp) is FenceVerdict.ACCEPTED

    def _operation(self, stamp: GenerationStamp) -> ProviderOperation:
        setup = self._setup
        return ProviderOperation(
            operation_id=self._ids.new_id(),
            logical_request_id=self._ids.new_id(),
            session_id=setup.session_id,
            turn_id=stamp.turn_id,
            component=OperationComponent.TTS,
            operation_type=SYNTHESIZE_OPERATION,
            provider=setup.provider,
            model=setup.model,
            worker_generation=setup.worker_generation,
        )

    def _request(self, track: SegmentTrack, operation: ProviderOperation) -> TtsSegmentRequest:
        return TtsSegmentRequest(
            stamp=track.stamp.for_operation(operation.operation_id),
            logical_request_id=operation.logical_request_id,
            segment_id=track.segment_id,
            sequence=track.index,
            text=track.text,
            language_code=track.segment.language_code,
            voice_configuration_version=self._setup.voice_configuration_version,
        )

    async def _attempt(self, track: SegmentTrack, operation: ProviderOperation) -> _Result:
        started = operation.transition_to(OperationStatus.STARTED, started_at=self._clock.utc_now())
        track.operation_ids.append(started.operation_id)
        self._fence.register_operation(started.operation_id)
        self._writer.submit(lambda: self._evidence.save_operation(started))
        self._writer.submit(lambda: self._started_event(track, started))
        request = self._request(track, started)
        try:
            result = await self._stream(track, request)
        finally:
            self._fence.retire_operation(started.operation_id)
        if isinstance(result.terminal, TtsSegmentCompleted) and not result.stale:
            track.synthesized = True
            await self._put(EndItem(stamp=track.stamp, track=track))
        self._settle(track, started, result)
        return result

    async def _stream(self, track: SegmentTrack, request: TtsSegmentRequest) -> _Result:
        result = _Result()
        started_ms = self._clock.monotonic_ms()
        events = self._tts.synthesize(request)
        try:
            async with asyncio.timeout(self._setup.attempt_guard_ms / MS_PER_SECOND):
                async for event in events:
                    if not result.stale and not self._current(event.stamp):
                        result.stale = True
                        await self._tts.cancel_segment(track.segment_id)
                    if isinstance(event, TtsAudioChunk):
                        await self._on_audio(track, result, event, started_ms)
                        continue
                    result.terminal = event
                    break
        except TimeoutError:
            result.terminal = _guard_failure(request, self._clock)
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
        result.total_ms = self._clock.monotonic_ms() - started_ms
        return result

    async def _on_audio(
        self,
        track: SegmentTrack,
        result: _Result,
        event: TtsAudioChunk,
        started_ms: int,
    ) -> None:
        if result.first_audio_ms is None:
            result.first_audio_ms = self._clock.monotonic_ms() - started_ms
            track.synthesized = True
        result.frames += 1
        if result.stale:
            track.late_frames += 1
            return
        track.frames_forwarded += 1
        await self._put(FrameItem(stamp=track.stamp, track=track, frame=event.frame))

    # ---------------------------------------------------------- evidence --
    async def _started_event(self, track: SegmentTrack, operation: ProviderOperation) -> None:
        await self._evidence.event(
            EventType.TTS_SEGMENT_STARTED,
            turn_id=operation.turn_id,
            operation_id=operation.operation_id,
            payload={
                "segment_sequence": track.segment.sequence,
                "piece_index": track.index,
                "attempt_number": operation.attempt_number,
                "normalized_characters": len(track.text),
                "language_code": track.segment.language_code.value,
            },
        )

    def _settle(self, track: SegmentTrack, operation: ProviderOperation, result: _Result) -> None:
        terminal = result.terminal
        if isinstance(terminal, TtsFailed):
            done, event_type = (
                operation.fail(terminal.failure, terminal.usage),
                EventType.TTS_FAILED,
            )
        elif isinstance(terminal, TtsSegmentCompleted) and not result.stale:
            done, event_type = operation.succeed(terminal.usage), EventType.TTS_SEGMENT_COMPLETED
        else:
            done, event_type = operation.cancel(result.usage), EventType.TTS_CANCELLED
        first_at = None
        if operation.started_at is not None and result.first_audio_ms is not None:
            first_at = operation.started_at + timedelta(milliseconds=result.first_audio_ms)
        settled = done.model_copy(
            update={
                "first_result_at": first_at,
                "time_to_first_result_ms": result.first_audio_ms,
                "total_duration_ms": result.total_ms,
                "result_summary": self._summary(track, result),
            }
        )
        self._writer.submit(lambda: self._evidence.operation_settled(settled))
        self._writer.submit(lambda: self._settled_events(track, settled, event_type, result))

    def _summary(self, track: SegmentTrack, result: _Result) -> dict[str, JsonValue]:
        summary: dict[str, JsonValue] = {
            "segment_sequence": track.segment.sequence,
            "piece_index": track.index,
            "original_characters": len(track.segment.text),
            "normalized_characters": len(track.text),
            "normalization_version": track.normalization_version,
            "language_code": track.segment.language_code.value,
            "voice_id": self._setup.voice_id,
            "generated_frames": result.frames,
            "forwarded_frames": track.frames_forwarded,
            "late_frames": track.late_frames,
            "generated_audio_ms": result.frames * RECOMMENDED_FRAME_MS,
            "first_audio_ms": result.first_audio_ms,
            "stale": result.stale,
        }
        terminal = result.terminal
        if isinstance(terminal, TtsSegmentCompleted) and terminal.provider_request_id:
            summary["provider_request_id"] = terminal.provider_request_id
        if track.segment.is_fallback:
            summary["fallback_template_id"] = track.segment.fallback_template_id
        return summary

    async def _settled_events(
        self,
        track: SegmentTrack,
        operation: ProviderOperation,
        event_type: EventType,
        result: _Result,
    ) -> None:
        turn_id, operation_id = operation.turn_id, operation.operation_id
        if result.first_audio_ms is not None:
            await self._evidence.event(
                EventType.TTS_FIRST_AUDIO,
                turn_id=turn_id,
                operation_id=operation_id,
                payload={"piece_index": track.index, "first_audio_ms": result.first_audio_ms},
            )
        payload: dict[str, JsonValue] = {"piece_index": track.index, "total_ms": result.total_ms}
        failure = result.failure
        if failure is not None:
            payload["error_type"] = failure.error_type.value
        severity = EventSeverity.WARNING if failure is not None else EventSeverity.INFO
        await self._evidence.event(
            event_type,
            turn_id=turn_id,
            operation_id=operation_id,
            payload=payload,
            severity=severity,
        )
        await self._evidence.event(
            EventType.TTS_USAGE,
            turn_id=turn_id,
            operation_id=operation_id,
            payload={"reporting_status": operation.usage.reporting_status.value},
        )


def _guard_failure(request: TtsSegmentRequest, clock: Clock) -> TtsFailed:
    failure = NormalizedFailure(
        component=ErrorComponent.TTS,
        error_type=ErrorType.PROVIDER_TIMEOUT,
        safe_message="Speech synthesis did not finish in time.",
        retryable=False,
        failure_phase="attempt_guard",
        session_id=request.stamp.session_id,
        turn_id=request.stamp.turn_id,
        operation_id=request.stamp.operation_id,
        occurred_at=clock.utc_now(),
        user_affected=True,
    )
    return TtsFailed(
        stamp=request.stamp,
        segment_id=request.segment_id,
        failure=failure,
        usage=UsageReport.unavailable(),
    )
