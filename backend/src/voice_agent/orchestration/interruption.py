"""Accepted barge-in in the canonical order (docs/01 §15, docs/05 §16, Decision 067 S9).

1. the Turn Manager confirmed the candidate (>=250 ms continuous speech);
2. increment the in-memory cancellation generation (the fence);
3. cancel LLM streaming and TTS: queued segments first, then the active one;
4. clear the application playback queue and the transport audio queue;
5. notify the browser (interruption state message);
6. record events and delivered-content evidence; the turn becomes ``interrupted``.

From step 2 every old-generation frame, delta, and acknowledgement is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_agent.contracts.enums import (
    InterruptionPhase,
    InterruptionReason,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.events import EventType, EventVisibility
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.state import (
    ActiveTurnState,
    SegmentStatus,
    SegmentTrack,
    SessionRuntime,
)
from voice_agent.orchestration.turn_lifecycle import finish_turn
from voice_agent.turn_management.turn_manager import AcceptInterruption


def interruption_phase(active: ActiveTurnState) -> InterruptionPhase:
    if active.audio_authorized:
        return InterruptionPhase.SPEAKING
    if active.segments:
        return InterruptionPhase.SYNTHESIZING
    return InterruptionPhase.THINKING


def delivered_accuracy(active: ActiveTurnState) -> SpokenTextAccuracy:
    """Full acknowledged segments are confirmed; any partial playback is estimated."""
    partial = any(
        t.status is not SegmentStatus.DELIVERED
        and (t.playback_started or t.status is SegmentStatus.PLAYING)
        for t in active.segments.values()
    )
    return SpokenTextAccuracy.ESTIMATED if partial else SpokenTextAccuracy.CONFIRMED


@dataclass(frozen=True, slots=True)
class _Cancelled:
    queued: list[SegmentTrack]
    in_flight: list[SegmentTrack]
    audible: list[SegmentTrack]
    llm_cancelled: bool


async def _cancel_generation_work(rt: SessionRuntime, active: ActiveTurnState) -> _Cancelled:
    """Step 3: LLM stream, then queued TTS segments, then the in-flight segment."""
    llm_cancelled = False
    if active.llm_operation_id is not None and not active.response_finished:
        await rt.ports.conversation.cancel(active.llm_operation_id)
        llm_cancelled = True
    drained = {segment.segment_id for segment in rt.segment_queue.clear()}
    queued = [
        track
        for track in active.segments.values()
        if track.segment.segment_id in drained or track.status is SegmentStatus.QUEUED
    ]
    for track in queued:
        track.status = SegmentStatus.CANCELLED
    in_flight = [
        t
        for t in active.segments.values()
        if t.status in {SegmentStatus.SYNTHESIZING, SegmentStatus.PLAYING}
    ]
    audible = [t for t in in_flight if t.status is SegmentStatus.PLAYING or t.playback_started]
    for track in in_flight:
        if not track.synthesis_done:
            await rt.ports.tts.cancel_segment(track.segment.segment_id)
        track.status = SegmentStatus.CANCELLED
    return _Cancelled(queued, in_flight, audible, llm_cancelled)


async def _clear_playback(rt: SessionRuntime) -> None:
    """Step 4: application playback queue, then ``AudioSource.clear_queue()``."""
    rt.playback_queue.clear()
    await rt.ports.transport.clear_playback()
    rt.ports.speech_activity.set_playback_active(False)


async def _record_cancellations(
    rt: SessionRuntime, active: ActiveTurnState, cancelled: _Cancelled
) -> None:
    turn_id = active.turn.turn_id
    if cancelled.llm_cancelled and active.llm_operation_id is not None:
        await _cancel_operation(rt, active.llm_operation_id)
        await rt.emit(
            EventType.CONVERSATION_CANCELLED,
            turn_id=turn_id,
            operation_id=active.llm_operation_id,
            component="conversation_engine",
            payload={"reason": InterruptionReason.USER_BARGE_IN.value},
        )
    for track in [*cancelled.queued, *cancelled.in_flight]:
        if track.operation_id is not None:
            await _cancel_operation(rt, track.operation_id)
        await rt.emit(
            EventType.TTS_CANCELLED,
            turn_id=turn_id,
            operation_id=track.operation_id,
            component="tts",
            payload={"segment_sequence": track.segment.sequence},
        )
    for track in cancelled.audible:
        await rt.emit(
            EventType.PLAYBACK_CANCELLED,
            turn_id=turn_id,
            component="playback",
            payload={"segment_sequence": track.segment.sequence},
        )


async def _cancel_operation(rt: SessionRuntime, operation_id: str) -> None:
    operation = rt.operations.get(operation_id)
    if operation is not None and not operation.is_terminal:
        await rt.save_operation(operation.cancel())


def _interrupted_turn(active: ActiveTurnState, phase: InterruptionPhase) -> ConversationTurn:
    turn = active.turn.record_spoken("", delivered_accuracy(active))
    return turn.interrupt(reason=InterruptionReason.USER_BARGE_IN, phase=phase)


async def accept_interruption(rt: SessionRuntime, decision: AcceptInterruption) -> None:
    active = rt.active
    if active is None or active.turn.is_terminal:
        return
    phase = interruption_phase(active)
    rt.fence.advance()
    cancelled = await _cancel_generation_work(rt, active)
    await _clear_playback(rt)
    turn = _interrupted_turn(active, phase)
    announcement = rt.factory.make(
        EventType.TURN_INTERRUPTED,
        component="turn_management",
        turn_id=turn.turn_id,
        visibility=EventVisibility.BROWSER_SAFE,
        payload={
            "status": TurnStatus.INTERRUPTED.value,
            "phase": phase.value,
            "accepted_at_ms": decision.accepted_at_ms,
        },
    )
    await rt.ports.transport.send_event(RealtimeTopic.STATE, announcement, reliable=True)
    await _record_cancellations(rt, active, cancelled)
    await finish_turn(rt, turn, announced=announcement, reset_turn_taking=False)
