"""Per-session child tasks: intake, STT/client readers, response, TTS, playback (docs/05 §8).

Tasks move data and submit commands; they never change user-visible state.
Every result is checked against the generation fence before it can move
toward playback, and checked again at playback (docs/05 §14). Once a result
is stale the task stops forwarding output and only harvests usage evidence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationFailed,
    ConversationRequest,
    ConversationTextDelta,
)
from voice_agent.contracts.enums import FinishReason, ResponseLanguage
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackFrame
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsFailed,
    TtsSegmentCompleted,
    TtsSegmentRequest,
)
from voice_agent.contracts.usage import UsageReport
from voice_agent.orchestration.commands import (
    AudioReceived,
    ClientEventReceived,
    InboundAudioOverflow,
    InputEnded,
    LateResultDiscarded,
    PlaybackAudio,
    PlaybackEnd,
    PlaybackPublished,
    QueuedSegment,
    ResponseFinished,
    ResponseTailDiscarded,
    ResponseTextReceived,
    SegmentQueued,
    SegmentRejected,
    SttEventReceived,
    TtsFinished,
    TtsStarted,
    WorkerCrashed,
)
from voice_agent.orchestration.generations import FenceVerdict
from voice_agent.orchestration.queues import QueueClosedError, QueueOverflowError
from voice_agent.orchestration.state import SessionRuntime
from voice_agent.response_segmentation.segmenter import (
    DiscardedTail,
    RejectedSegment,
    SegmenterState,
    SegmentOutcome,
    SpeakableSegment,
    feed,
    finish,
)


async def _close_iterator(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


async def guard(rt: SessionRuntime, name: str, work: Awaitable[None]) -> None:
    """Report a crashed child task to the orchestrator instead of mutating state."""
    try:
        await work
    except QueueClosedError:
        return
    except Exception as exc:
        await rt.inbox.put(WorkerCrashed(task_name=name, error_class=type(exc).__name__))


async def _late(rt: SessionRuntime, source: str, verdict: FenceVerdict) -> None:
    await rt.inbox.put(LateResultDiscarded(source=source, verdict=verdict))


async def read_transport_audio(rt: SessionRuntime) -> None:
    """Transport frames -> bounded inbound queue; overflow is evidence, never silent."""
    frames = rt.ports.transport.audio_frames()
    overflowing = False
    try:
        async for frame in frames:
            try:
                await rt.inbound_audio.put(frame)
                overflowing = False
            except QueueOverflowError:
                if not overflowing:
                    overflowing = True
                    await rt.inbox.put(InboundAudioOverflow(at_ms=frame.captured_at_ms))
    finally:
        await _close_iterator(frames)
        rt.inbound_audio.close()


async def forward_inbound_audio(rt: SessionRuntime) -> None:
    while True:
        try:
            frame = await rt.inbound_audio.get()
        except QueueClosedError:
            await rt.inbox.put(InputEnded())
            return
        await rt.inbox.put(AudioReceived(frame=frame))


async def read_stt_events(rt: SessionRuntime) -> None:
    events = rt.ports.stt.events()
    try:
        async for event in events:
            await rt.inbox.put(SttEventReceived(event=event))
    finally:
        await _close_iterator(events)


async def read_client_events(rt: SessionRuntime) -> None:
    events = rt.ports.transport.client_events()
    try:
        async for event in events:
            await rt.inbox.put(ClientEventReceived(event=event))
    finally:
        await _close_iterator(events)


@dataclass(frozen=True, slots=True)
class _ResponseContext:
    op_stamp: GenerationStamp
    turn_stamp: GenerationStamp
    logical_request_id: str


async def produce_response(
    rt: SessionRuntime, request: ConversationRequest, language: ResponseLanguage | None
) -> None:
    """Conversation deltas -> ResponseSegmenter -> bounded segment queue (backpressure)."""
    ctx = _ResponseContext(
        op_stamp=request.stamp,
        turn_stamp=request.stamp.model_copy(update={"operation_id": None}),
        logical_request_id=request.logical_request_id,
    )
    state = SegmenterState(turn_language=language)
    events = rt.ports.conversation.stream(request)
    forwarding = True
    try:
        async for event in events:
            verdict = rt.fence.check(event.stamp)
            if forwarding and verdict is not FenceVerdict.ACCEPTED:
                forwarding = False
                await _late(rt, "conversation", verdict)
            if isinstance(event, ConversationTextDelta):
                if forwarding:
                    await rt.inbox.put(ResponseTextReceived(stamp=ctx.op_stamp, text=event.text))
                    state, outcomes = feed(state, event.text)
                    forwarding = await enqueue_outcomes(
                        rt, ctx.turn_stamp, ctx.logical_request_id, outcomes
                    )
            elif isinstance(
                event, ConversationCompleted | ConversationCancelled | ConversationFailed
            ):
                await _finish_response(rt, ctx, state, event, forwarding=forwarding)
                return
        await rt.inbox.put(_stream_ended_without_completion(rt, ctx.op_stamp))
    finally:
        await _close_iterator(events)


async def _finish_response(
    rt: SessionRuntime,
    ctx: _ResponseContext,
    state: SegmenterState,
    event: ConversationCompleted | ConversationCancelled | ConversationFailed,
    *,
    forwarding: bool,
) -> None:
    if isinstance(event, ConversationCompleted):
        if forwarding:
            _, outcomes = finish(state, event.finish_reason)
            await enqueue_outcomes(rt, ctx.turn_stamp, ctx.logical_request_id, outcomes)
        finished = ResponseFinished(
            stamp=ctx.op_stamp, finish_reason=event.finish_reason, usage=event.usage
        )
    elif isinstance(event, ConversationCancelled):
        finished = ResponseFinished(
            stamp=ctx.op_stamp,
            finish_reason=FinishReason.CANCELLED,
            usage=event.usage,
            cancelled=True,
        )
    else:
        finished = ResponseFinished(
            stamp=ctx.op_stamp,
            finish_reason=FinishReason.ERROR,
            usage=event.usage,
            failure=event.failure,
        )
    await rt.inbox.put(finished)


def _stream_ended_without_completion(
    rt: SessionRuntime, stamp: GenerationStamp
) -> ResponseFinished:
    failure = NormalizedFailure(
        component=ErrorComponent.CONVERSATION_ENGINE,
        error_type=ErrorType.UNKNOWN_PROVIDER_ERROR,
        safe_message="conversation stream ended without completion",
        retryable=False,
        session_id=stamp.session_id,
        turn_id=stamp.turn_id,
        operation_id=stamp.operation_id,
        occurred_at=rt.ports.clock.utc_now(),
    )
    return ResponseFinished(
        stamp=stamp,
        finish_reason=FinishReason.ERROR,
        usage=UsageReport.unavailable(),
        failure=failure,
    )


async def enqueue_outcomes(
    rt: SessionRuntime,
    stamp: GenerationStamp,
    logical_request_id: str,
    outcomes: list[SegmentOutcome],
    *,
    is_fallback: bool = False,
) -> bool:
    """Submit segmenter outcomes; returns ``False`` once the stamp is stale."""
    for outcome in outcomes:
        verdict = rt.fence.check(stamp)
        if verdict is not FenceVerdict.ACCEPTED:
            await _late(rt, "segment", verdict)
            return False
        if isinstance(outcome, SpeakableSegment):
            segment = QueuedSegment(
                stamp=stamp,
                logical_request_id=logical_request_id,
                segment_id=rt.new_id(),
                sequence=outcome.sequence,
                text=outcome.text,
                language_code=outcome.language_code,
                is_fallback=is_fallback,
            )
            await rt.inbox.put(SegmentQueued(segment=segment))
            await rt.segment_queue.put(segment)
        elif isinstance(outcome, RejectedSegment):
            await rt.inbox.put(SegmentRejected(stamp, outcome.sequence, outcome.reason))
        elif isinstance(outcome, DiscardedTail):
            await rt.inbox.put(ResponseTailDiscarded(stamp=stamp))
    return True


async def synthesize_segments(rt: SessionRuntime) -> None:
    while True:
        try:
            segment = await rt.segment_queue.get()
        except QueueClosedError:
            return
        await _synthesize_one(rt, segment)


async def _synthesize_one(rt: SessionRuntime, segment: QueuedSegment) -> None:
    verdict = rt.fence.check(segment.stamp)
    if verdict is not FenceVerdict.ACCEPTED:
        await _late(rt, "tts_request", verdict)
        return
    operation_id = rt.new_id()
    rt.fence.register_operation(operation_id)
    op_stamp = segment.stamp.for_operation(operation_id)
    await rt.inbox.put(TtsStarted(stamp=op_stamp, segment=segment))
    request = TtsSegmentRequest(
        stamp=op_stamp,
        logical_request_id=segment.logical_request_id,
        segment_id=segment.segment_id,
        sequence=segment.sequence,
        text=segment.text,
        language_code=segment.language_code,
        voice_configuration_version=rt.settings.voice.configuration_version,
    )
    try:
        finished = await _stream_tts(rt, request, segment)
    finally:
        rt.fence.retire_operation(operation_id)
    await rt.inbox.put(finished)


async def _stream_tts(
    rt: SessionRuntime, request: TtsSegmentRequest, segment: QueuedSegment
) -> TtsFinished:
    identity = PlaybackAckIdentity(
        worker_generation=segment.stamp.worker_generation,
        cancellation_generation=segment.stamp.cancellation_generation,
        segment_id=segment.segment_id,
    )
    turn_id = segment.stamp.turn_id or ""
    forwarding = True
    events = rt.ports.tts.synthesize(request)
    try:
        async for event in events:
            verdict = rt.fence.check(event.stamp)
            if forwarding and verdict is not FenceVerdict.ACCEPTED:
                forwarding = False
                await _late(rt, "tts", verdict)
            if isinstance(event, TtsAudioChunk) and forwarding:
                item = PlaybackAudio(segment.stamp, identity, turn_id, event.frame)
                await rt.playback_queue.put(item)
            elif isinstance(event, TtsSegmentCompleted):
                if forwarding:
                    await rt.playback_queue.put(PlaybackEnd(stamp=segment.stamp, identity=identity))
                return TtsFinished(request.stamp, segment, event.usage, cancelled=not forwarding)
            elif isinstance(event, TtsCancelled):
                return TtsFinished(request.stamp, segment, event.usage, cancelled=True)
            elif isinstance(event, TtsFailed):
                return TtsFinished(request.stamp, segment, event.usage, failure=event.failure)
    finally:
        await _close_iterator(events)
    return TtsFinished(request.stamp, segment, UsageReport.unavailable(), cancelled=True)


async def publish_playback(rt: SessionRuntime) -> None:
    """Final authorization point: every frame rechecks the generation (docs/05 §14)."""
    started: set[str] = set()
    while True:
        try:
            item = await rt.playback_queue.get()
        except QueueClosedError:
            return
        verdict = rt.fence.check(item.stamp)
        if verdict is not FenceVerdict.ACCEPTED:
            await _late(rt, "playback", verdict)
            continue
        if isinstance(item, PlaybackEnd):
            await rt.ports.transport.finish_segment(item.identity)
            continue
        frame = PlaybackFrame(identity=item.identity, turn_id=item.turn_id, frame=item.frame)
        await rt.ports.transport.publish_audio(frame)
        if item.identity.segment_id not in started:
            started.add(item.identity.segment_id)
            await rt.inbox.put(
                PlaybackPublished(stamp=item.stamp, segment_id=item.identity.segment_id)
            )
