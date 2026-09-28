"""Idempotent session finalization (docs/05 §20, §22).

Every end path (normal end, failure, worker crash, input end) shares this
guarded sequence: block turns, fence output, stop playback, cancel
generation, close STT/TTS/conversation/transport, finalize the open turn,
record final usage and cost (missing usage stays unavailable), then write
the terminal session state. A failing close step never stops the rest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from voice_agent.contracts.enums import (
    DisconnectReason,
    InterruptionPhase,
    InterruptionReason,
    OperationStatus,
    SessionStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageReport
from voice_agent.costing.calculator import CostCalculator
from voice_agent.costing.usage_normalization import merge_reports
from voice_agent.domain.session import VoiceSession
from voice_agent.orchestration.state import SessionRuntime
from voice_agent.ports.costing import MeteredUsage


async def _safely(rt: SessionRuntime, step: str, work: Awaitable[None]) -> None:
    try:
        await work
    except Exception:
        failure = NormalizedFailure(
            component=ErrorComponent.ORCHESTRATOR,
            error_type=ErrorType.INTERNAL_ERROR,
            safe_message=f"finalization step failed: {step}",
            retryable=False,
            failure_phase="shutdown",
            session_id=rt.session.session_id,
            occurred_at=rt.ports.clock.utc_now(),
        )
        await rt.record_failure(failure)


async def _stop_tasks(rt: SessionRuntime) -> None:
    for queue in (rt.inbound_audio, rt.segment_queue, rt.playback_queue, rt.inbox):
        queue.close()
    current = asyncio.current_task()
    tasks = [task for task in rt.tasks if task is not current and not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _cancel_active_generation(rt: SessionRuntime) -> None:
    active = rt.active
    if active is None:
        return
    if active.llm_operation_id is not None and not active.response_finished:
        await _safely(
            rt, "conversation.cancel", rt.ports.conversation.cancel(active.llm_operation_id)
        )
    await _safely(rt, "tts.cancel_turn", rt.ports.tts.cancel_turn(active.turn.turn_id))


async def _close_adapters(rt: SessionRuntime) -> None:
    await _safely(rt, "stt.close", rt.ports.stt.close())
    await _safely(rt, "tts.close", rt.ports.tts.close())
    await _safely(rt, "conversation.close", rt.ports.conversation.close())
    await _safely(rt, "transport.close", rt.ports.transport.close())


async def _finalize_open_turn(rt: SessionRuntime) -> None:
    active = rt.active
    if active is None or active.turn.is_terminal:
        return
    turn = active.turn
    if turn.status is TurnStatus.AUDIO_STREAMING:
        turn = turn.interrupt(
            reason=InterruptionReason.SESSION_END, phase=InterruptionPhase.SPEAKING
        )
        event_type = EventType.TURN_INTERRUPTED
    else:
        turn = turn.abandon()
        event_type = EventType.TURN_ABANDONED
    for operation in list(rt.operations.values()):
        if operation.turn_id == turn.turn_id and not operation.is_terminal:
            await rt.save_operation(operation.cancel())
    await rt.save_turn(turn)
    rt.finished_turns.append(turn)
    rt.active = None
    await rt.emit(event_type, turn_id=turn.turn_id, payload={"status": turn.status.value})


async def _close_stt_operation(rt: SessionRuntime) -> None:
    operation = rt.operations.get(rt.stt_operation_id or "")
    if operation is None or operation.is_terminal:
        return
    usage = merge_reports(rt.stt_usage) if rt.stt_usage else UsageReport.unavailable()
    if operation.status is OperationStatus.STARTED:
        operation = operation.transition_to(OperationStatus.STREAMING)
    await rt.save_operation(operation.succeed(usage))
    await rt.emit(EventType.STT_STREAM_CLOSED, operation_id=operation.operation_id, component="stt")


async def _record_cost(rt: SessionRuntime) -> None:
    usages = [
        MeteredUsage(op.component, op.provider, op.model, op.usage) for op in rt.operations.values()
    ]
    calculation = CostCalculator(rt.settings.rate_card).calculate(
        usages, reporting_currency=rt.settings.reporting_currency
    )
    run_id = rt.new_id()
    await rt.ports.cost_entries.add_calculation(rt.session.session_id, run_id, calculation)
    rt.cost = calculation
    await rt.emit(
        EventType.COST_CALCULATED,
        component="cost_engine",
        payload={
            "calculation_run_id": run_id,
            "calculation_status": calculation.status.value,
            "currency": calculation.reporting_currency.value,
            "rate_card_id": calculation.rate_card_id,
            "total": None if calculation.total is None else str(calculation.total),
            "missing_count": len(calculation.missing),
        },
    )


async def _enter_ending(rt: SessionRuntime, reason: DisconnectReason) -> None:
    session = rt.session
    if session.can_transition_to(SessionStatus.ENDING):
        await rt.save_session(session.transition_to(SessionStatus.ENDING, disconnect_reason=reason))
        await rt.emit(EventType.SESSION_ENDING, payload={"reason": reason.value})


async def finalize(rt: SessionRuntime, reason: DisconnectReason, *, failed: bool) -> VoiceSession:
    """Run the shared end path once; repeated calls return the existing outcome."""
    if rt.finalized:
        return rt.session
    rt.finalized = True
    await _enter_ending(rt, reason)
    rt.fence.revoke()
    rt.segment_queue.clear()
    rt.playback_queue.clear()
    await _safely(rt, "transport.clear_playback", rt.ports.transport.clear_playback())
    await _cancel_active_generation(rt)
    await _stop_tasks(rt)
    await _close_adapters(rt)
    await _finalize_open_turn(rt)
    await _close_stt_operation(rt)
    await _record_cost(rt)
    target = SessionStatus.FAILED if failed else SessionStatus.ENDED
    if rt.session.can_transition_to(target):
        await rt.save_session(rt.session.transition_to(target))
    terminal = (
        EventType.SESSION_FAILED if target is SessionStatus.FAILED else EventType.SESSION_ENDED
    )
    await rt.emit(terminal, payload={"reason": reason.value})
    return rt.session
