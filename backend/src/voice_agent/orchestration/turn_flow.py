"""Command-loop handlers for one user turn (docs/05 §12-§15).

Only the orchestrator loop calls these. Each handler re-checks the
generation fence before changing turn state; results that fail the check
are counted as ``discarded_late`` and only contribute usage evidence.
"""

from __future__ import annotations

import asyncio

from voice_agent.contracts.conversation import (
    MAX_HISTORY_MESSAGES,
    ConversationRequest,
    HistoryMessage,
    HistoryRole,
)
from voice_agent.contracts.enums import (
    AgentActivityState,
    FinishReason,
    InputDisposition,
    OperationComponent,
    OperationStatus,
    ResponseCompletionStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.stt import SttTurnFinalized
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.commands import (
    QueuedSegment,
    ResponseFinished,
    ResponseTextReceived,
    SegmentQueued,
    SegmentRejected,
)
from voice_agent.orchestration.pipeline import guard, produce_response
from voice_agent.orchestration.state import (
    ActiveTurnState,
    SegmentTrack,
    SessionRuntime,
)
from voice_agent.orchestration.turn_lifecycle import (
    current_turn,
    fail_turn,
    finish_turn,
    internal_failure,
    maybe_complete,
    settle_operation,
)
from voice_agent.response_segmentation.language import classify_turn_language
from voice_agent.response_segmentation.segmenter import (
    SegmenterState,
    SpeakableSegment,
    feed,
    finish,
)
from voice_agent.turn_management.fallbacks import (
    RESPONSE_TRUNCATED,
    UNCLEAR_INPUT,
    FallbackTemplate,
)


def bounded_history(history: list[HistoryMessage]) -> tuple[HistoryMessage, ...]:
    """Keep the newest messages within the contract bound, starting on a user message.

    Token-budget truncation (12,000-token history target) belongs to the WP8
    adapter work; this only guarantees the request contract bound.
    """
    recent = history[-MAX_HISTORY_MESSAGES:]
    while recent and recent[0].role is not HistoryRole.USER:
        recent = recent[1:]
    return tuple(recent)


async def open_turn(rt: SessionRuntime, speech_started_at_ms: int) -> None:
    if rt.finalized or rt.active is not None:
        return
    rt.turn_count += 1
    turn = ConversationTurn(
        turn_id=rt.new_id(), session_id=rt.session.session_id, sequence_number=rt.turn_count
    )
    rt.active = ActiveTurnState(turn=turn, speech_started_at_ms=speech_started_at_ms)
    rt.fence.activate_turn(turn.turn_id)
    await rt.save_turn(turn)
    rt.set_activity(AgentActivityState.LISTENING)
    await rt.emit(
        EventType.TURN_OPENED, turn_id=turn.turn_id, payload={"sequence_number": rt.turn_count}
    )
    await rt.emit(
        EventType.USER_SPEECH_STARTED,
        turn_id=turn.turn_id,
        component="speech_activity",
        payload={"speech_started_at_ms": speech_started_at_ms},
    )


async def commit_endpoint(rt: SessionRuntime, last_speech_at_ms: int, committed_at_ms: int) -> None:
    active = rt.active
    if active is None:
        return
    rt.set_activity(AgentActivityState.TRANSCRIBING)
    await rt.emit(
        EventType.USER_SPEECH_ENDED,
        turn_id=active.turn.turn_id,
        component="speech_activity",
        payload={"last_speech_at_ms": last_speech_at_ms, "committed_at_ms": committed_at_ms},
    )
    stamp = rt.fence.stamp(turn_id=active.turn.turn_id, operation_id=rt.stt_operation_id)
    await rt.ports.stt.finalize_turn(stamp)


async def on_transcript(rt: SessionRuntime, event: SttTurnFinalized) -> None:
    active = current_turn(rt, event.stamp)
    if active is None:
        rt.count_late("stt")
        return
    await rt.emit(
        EventType.STT_TURN_FINALIZED,
        turn_id=active.turn.turn_id,
        operation_id=rt.stt_operation_id,
        component="stt",
        payload={"usable": event.is_usable, "character_count": len(event.text)},
    )
    if not event.is_usable:
        turn = await rt.save_turn(active.turn.reject_input(InputDisposition.EMPTY))
        if rt.settings.clarification_fallback_enabled:
            await speak_fallback(rt, UNCLEAR_INPUT)
            return
        await finish_turn(rt, turn.discard())
        return
    language = classify_turn_language(event.text, event.language)
    # The final transcript is durable before generation is authorized (docs/05 §23).
    await rt.save_turn(active.turn.accept_transcript(event.text, language))
    await start_response(rt, active)


async def start_response(rt: SessionRuntime, active: ActiveTurnState) -> None:
    identity = rt.settings.conversation_identity
    profile = rt.settings.conversation
    operation = ProviderOperation(
        operation_id=rt.new_id(),
        logical_request_id=rt.new_id(),
        session_id=rt.session.session_id,
        turn_id=active.turn.turn_id,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider=identity.provider,
        model=identity.model,
        worker_generation=rt.settings.worker_generation,
    ).transition_to(OperationStatus.STARTED)
    await rt.save_operation(operation)
    rt.fence.register_operation(operation.operation_id)
    active.llm_operation_id = operation.operation_id
    active.logical_request_id = operation.logical_request_id
    turn = await rt.save_turn(active.turn.start_response())
    rt.set_activity(AgentActivityState.THINKING)
    request = ConversationRequest(
        stamp=rt.fence.stamp(turn_id=turn.turn_id, operation_id=operation.operation_id),
        logical_request_id=operation.logical_request_id,
        correlation_id=rt.session.correlation_id,
        agent_config_id=profile.agent_config_id,
        config_checksum=profile.config_checksum,
        system_instruction_id=profile.system_instruction_id,
        system_instruction_version=profile.system_instruction_version,
        system_instruction=profile.system_instruction,
        user_transcript=turn.final_transcript or "",
        history=bounded_history(rt.history),
        language=turn.language,
        max_output_tokens=profile.max_output_tokens,
    )
    await rt.emit(
        EventType.CONVERSATION_STARTED,
        turn_id=turn.turn_id,
        operation_id=operation.operation_id,
        component="conversation_engine",
    )
    task = asyncio.create_task(guard(rt, "response", produce_response(rt, request, turn.language)))
    active.response_task = task
    rt.tasks.append(task)


async def on_response_text(rt: SessionRuntime, command: ResponseTextReceived) -> None:
    active = current_turn(rt, command.stamp)
    if active is None:
        rt.count_late("conversation_text")
        return
    first = not active.turn.generated_text
    active.turn = active.turn.record_generated(command.text)
    if first:
        await rt.emit(
            EventType.CONVERSATION_FIRST_TOKEN,
            turn_id=active.turn.turn_id,
            operation_id=command.stamp.operation_id,
            component="conversation_engine",
        )


async def _track_segment(
    rt: SessionRuntime, active: ActiveTurnState, segment: QueuedSegment
) -> None:
    active.segments[segment.segment_id] = SegmentTrack(segment=segment)
    await rt.emit(
        EventType.CONVERSATION_SEGMENT_READY,
        turn_id=active.turn.turn_id,
        component="response_segmentation",
        payload={
            "segment_sequence": segment.sequence,
            "language_code": segment.language_code.value,
            "character_count": len(segment.text),
            "is_fallback": segment.is_fallback,
        },
    )


async def on_segment_queued(rt: SessionRuntime, command: SegmentQueued) -> None:
    active = current_turn(rt, command.segment.stamp)
    if active is None:
        rt.count_late("segment")
        return
    await _track_segment(rt, active, command.segment)


async def on_segment_rejected(rt: SessionRuntime, command: SegmentRejected) -> None:
    failure = NormalizedFailure(
        component=ErrorComponent.ORCHESTRATOR,
        error_type=ErrorType.CONTENT_REJECTED,
        safe_message=f"segment rejected before TTS: {command.reason.value}",
        retryable=False,
        session_id=rt.session.session_id,
        turn_id=command.stamp.turn_id,
        occurred_at=rt.ports.clock.utc_now(),
    )
    await rt.record_failure(failure)


async def on_response_finished(rt: SessionRuntime, command: ResponseFinished) -> None:
    operation_id = command.stamp.operation_id or ""
    operation = rt.operations.get(operation_id)
    if operation is not None:
        await rt.save_operation(
            settle_operation(
                operation, command.usage, failure=command.failure, cancelled=command.cancelled
            )
        )
    await rt.emit(
        EventType.CONVERSATION_USAGE,
        turn_id=command.stamp.turn_id,
        operation_id=operation_id,
        component="conversation_engine",
        payload={"reporting_status": command.usage.reporting_status.value},
    )
    active = current_turn(rt, command.stamp)
    rt.fence.retire_operation(operation_id)
    if active is None or active.llm_operation_id != operation_id:
        rt.count_late("conversation_finished")
        return
    await _apply_response_finish(rt, active, command)


async def _apply_response_finish(
    rt: SessionRuntime, active: ActiveTurnState, command: ResponseFinished
) -> None:
    active.response_finished = True
    active.finish_reason = command.finish_reason
    if command.finish_reason is not None:
        active.turn = active.turn.record_finish_reason(command.finish_reason)
    event_type = EventType.CONVERSATION_COMPLETED
    if command.failure is not None:
        event_type = EventType.CONVERSATION_FAILED
    elif command.cancelled:
        event_type = EventType.CONVERSATION_CANCELLED
    await rt.emit(
        event_type,
        turn_id=active.turn.turn_id,
        operation_id=command.stamp.operation_id,
        component="conversation_engine",
        payload={"finish_reason": (command.finish_reason or FinishReason.ERROR).value},
    )
    if command.failure is not None or command.cancelled:
        await fail_turn(rt, command.failure or internal_failure(rt, "conversation cancelled"))
        return
    if command.finish_reason is FinishReason.MAXIMUM_TOKENS:
        if not active.segments:
            await speak_fallback(
                rt, RESPONSE_TRUNCATED, ResponseCompletionStatus.TRUNCATED_FALLBACK
            )
            return
        active.completion_override = ResponseCompletionStatus.TRUNCATED_PARTIAL
    await maybe_complete(rt)


async def speak_fallback(
    rt: SessionRuntime,
    template: FallbackTemplate,
    completion: ResponseCompletionStatus | None = None,
) -> None:
    """Send an approved deterministic phrase through the normal segmentation/TTS path."""
    active = rt.active
    if active is None:
        return
    active.fallback_active = True
    active.response_finished = True
    active.completion_override = completion
    state = SegmenterState(turn_language=active.turn.language)
    state, outcomes = feed(state, template.text)
    _, tail = finish(state, FinishReason.COMPLETED)
    stamp = rt.fence.stamp(turn_id=active.turn.turn_id)
    logical_request_id = rt.new_id()
    for outcome in [*outcomes, *tail]:
        if not isinstance(outcome, SpeakableSegment):
            continue
        segment = QueuedSegment(
            stamp=stamp,
            logical_request_id=logical_request_id,
            segment_id=rt.new_id(),
            sequence=outcome.sequence,
            text=outcome.text,
            language_code=outcome.language_code,
            is_fallback=True,
        )
        await _track_segment(rt, active, segment)
        await rt.segment_queue.put(segment)
