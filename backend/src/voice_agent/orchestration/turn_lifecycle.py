"""Shared turn-lifecycle helpers: fence checks, completion, failure, and terminal evidence.

Only the orchestrator command loop calls these (docs/05 §12-§15, §13 history).
"""

from __future__ import annotations

from voice_agent.contracts.conversation import HistoryMessage, HistoryRole
from voice_agent.contracts.enums import (
    AgentActivityState,
    InputDisposition,
    OperationStatus,
    ResponseCompletionStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.contracts.usage import UsageReport
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.state import (
    ActiveTurnState,
    SegmentStatus,
    SessionRuntime,
)
from voice_agent.turn_management.turn_manager import agent_response_finished

_TERMINAL_EVENT = {
    TurnStatus.COMPLETED: EventType.TURN_COMPLETED,
    TurnStatus.INTERRUPTED: EventType.TURN_INTERRUPTED,
    TurnStatus.FAILED: EventType.TURN_FAILED,
    TurnStatus.ABANDONED: EventType.TURN_ABANDONED,
    TurnStatus.DISCARDED: EventType.TURN_DISCARDED,
}


def current_turn(rt: SessionRuntime, stamp: GenerationStamp) -> ActiveTurnState | None:
    active = rt.active
    if active is None or stamp.turn_id != active.turn.turn_id or not rt.fence.accepts(stamp):
        return None
    return active


def settle_operation(
    operation: ProviderOperation,
    usage: UsageReport,
    *,
    failure: NormalizedFailure | None,
    cancelled: bool,
) -> ProviderOperation:
    if operation.is_terminal:
        return operation.with_late_usage(usage)
    if failure is not None:
        return operation.fail(failure, usage)
    if cancelled:
        return operation.cancel(usage)
    if operation.status is OperationStatus.STARTED:
        operation = operation.transition_to(OperationStatus.STREAMING)
    return operation.succeed(usage)


def internal_failure(rt: SessionRuntime, message: str) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.ORCHESTRATOR,
        error_type=ErrorType.INTERNAL_ERROR,
        safe_message=message,
        retryable=False,
        session_id=rt.session.session_id,
        turn_id=rt.active.turn.turn_id if rt.active else None,
        occurred_at=rt.ports.clock.utc_now(),
    )


async def maybe_complete(rt: SessionRuntime) -> None:
    active = rt.active
    if active is None or not active.response_finished or active.open_segments():
        return
    turn = active.turn
    completion = active.completion_override or ResponseCompletionStatus.COMPLETED
    failed_audio = any(t.status is SegmentStatus.FAILED for t in active.segments.values())
    if failed_audio and active.delivered_count() == 0:
        # Nothing was delivered, so the turn failed (docs/05 §18, TTS behaviour).
        await fail_turn(rt, internal_failure(rt, "no agent audio was delivered"))
        return
    if turn.status is TurnStatus.AUDIO_STREAMING:
        await finish_turn(rt, turn.complete(completion))
    elif turn.status is TurnStatus.RESPONSE_STREAMING and active.delivered_count() == 0:
        await finish_turn(rt, turn.complete(completion, no_speakable_output=True))


async def fail_turn(rt: SessionRuntime, failure: NormalizedFailure, *, record: bool = True) -> None:
    active = rt.active
    if active is None or active.turn.is_terminal:
        return
    if record:
        await rt.record_failure(failure)
    await finish_turn(
        rt, active.turn.fail(), payload_extra={"error_type": failure.error_type.value}
    )


def _append_history(rt: SessionRuntime, turn: ConversationTurn) -> None:
    if turn.input_disposition is not InputDisposition.ACCEPTED or not turn.final_transcript:
        return
    rt.history.append(
        HistoryMessage(role=HistoryRole.USER, text=turn.final_transcript, turn_id=turn.turn_id)
    )
    if turn.spoken_text.strip():
        rt.history.append(
            HistoryMessage(role=HistoryRole.ASSISTANT, text=turn.spoken_text, turn_id=turn.turn_id)
        )


async def finish_turn(
    rt: SessionRuntime,
    turn: ConversationTurn,
    *,
    announced: EventEnvelope | None = None,
    reset_turn_taking: bool = True,
    payload_extra: dict[str, str] | None = None,
) -> None:
    """Persist a terminal turn, update delivered history, and release output authority."""
    await rt.save_turn(turn)
    _append_history(rt, turn)
    rt.finished_turns.append(turn)
    rt.fence.deactivate_turn(turn.turn_id)
    rt.active = None
    if reset_turn_taking:
        rt.turn_taking = agent_response_finished(rt.turn_taking)
    rt.ports.speech_activity.set_playback_active(False)
    rt.set_activity(AgentActivityState.LISTENING)
    if announced is not None:
        await rt.ports.events.publish(announced)
        return
    payload: dict[str, str] = {
        "status": turn.status.value,
        "response_completion_status": turn.response_completion_status.value,
        **(payload_extra or {}),
    }
    await rt.emit(
        _TERMINAL_EVENT[turn.status],
        turn_id=turn.turn_id,
        payload=dict(payload),
        browser_topic=RealtimeTopic.STATE,
    )
