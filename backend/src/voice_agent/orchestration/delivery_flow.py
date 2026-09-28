"""Command-loop handlers for TTS synthesis, playback publication, and acknowledgements.

Playback acknowledgements are evidence, never authority (docs/06 §13); every
handler re-checks the generation fence before changing turn state.
"""

from __future__ import annotations

from voice_agent.contracts.enums import (
    AgentActivityState,
    OperationComponent,
    OperationStatus,
    SpokenTextAccuracy,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.transport import PlaybackAck, PlaybackAckKind
from voice_agent.domain.operation import ProviderOperation
from voice_agent.orchestration.commands import (
    PlaybackPublished,
    TtsFinished,
    TtsStarted,
)
from voice_agent.orchestration.state import (
    ActiveTurnState,
    SegmentStatus,
    SegmentTrack,
    SessionRuntime,
)
from voice_agent.orchestration.turn_lifecycle import (
    current_turn,
    fail_turn,
    internal_failure,
    maybe_complete,
    settle_operation,
)


async def on_tts_started(rt: SessionRuntime, command: TtsStarted) -> None:
    identity = rt.settings.tts_identity
    segment = command.segment
    operation = ProviderOperation(
        operation_id=command.stamp.operation_id or rt.new_id(),
        logical_request_id=segment.logical_request_id,
        session_id=rt.session.session_id,
        turn_id=segment.stamp.turn_id,
        component=OperationComponent.TTS,
        operation_type="synthesize_stream",
        provider=identity.provider,
        model=identity.model,
        worker_generation=rt.settings.worker_generation,
    ).transition_to(OperationStatus.STARTED)
    await rt.save_operation(operation)
    active = current_turn(rt, segment.stamp)
    track = active.segments.get(segment.segment_id) if active else None
    if active is None or track is None:
        rt.count_late("tts_started")
        return
    track.status = SegmentStatus.SYNTHESIZING
    track.operation_id = operation.operation_id
    active.turn = active.turn.record_synthesized(segment.text)
    await rt.emit(
        EventType.TTS_SEGMENT_STARTED,
        turn_id=active.turn.turn_id,
        operation_id=operation.operation_id,
        component="tts",
        payload={"segment_sequence": segment.sequence},
    )


async def on_tts_finished(rt: SessionRuntime, command: TtsFinished) -> None:
    operation = rt.operations.get(command.stamp.operation_id or "")
    if operation is not None:
        settled = settle_operation(
            operation, command.usage, failure=command.failure, cancelled=command.cancelled
        )
        await rt.save_operation(settled)
    await rt.emit(
        EventType.TTS_USAGE,
        turn_id=command.segment.stamp.turn_id,
        operation_id=command.stamp.operation_id,
        component="tts",
        payload={"reporting_status": command.usage.reporting_status.value},
    )
    active = current_turn(rt, command.segment.stamp)
    track = active.segments.get(command.segment.segment_id) if active else None
    if active is None or track is None:
        rt.count_late("tts_finished")
        return
    track.synthesis_done = True
    if command.failure is not None or command.cancelled:
        track.status = SegmentStatus.FAILED
        if active.delivered_count() == 0:
            await fail_turn(rt, command.failure or internal_failure(rt, "tts cancelled"))
            return
    else:
        await rt.emit(
            EventType.TTS_SEGMENT_COMPLETED,
            turn_id=active.turn.turn_id,
            operation_id=command.stamp.operation_id,
            component="tts",
            payload={"segment_sequence": command.segment.sequence},
        )
    await maybe_complete(rt)


async def on_playback_published(rt: SessionRuntime, command: PlaybackPublished) -> None:
    active = current_turn(rt, command.stamp)
    track = active.segments.get(command.segment_id) if active else None
    if active is None or track is None:
        rt.count_late("playback_published")
        return
    track.status = SegmentStatus.PLAYING
    if not active.audio_authorized:
        active.audio_authorized = True
        await rt.save_turn(active.turn.authorize_audio(fallback=active.fallback_active))
        rt.set_activity(AgentActivityState.SPEAKING)
        rt.ports.speech_activity.set_playback_active(True)
    await rt.emit(
        EventType.TTS_FIRST_AUDIO,
        turn_id=active.turn.turn_id,
        operation_id=track.operation_id,
        component="tts",
        payload={"segment_sequence": track.segment.sequence},
    )


async def on_playback_ack(rt: SessionRuntime, ack: PlaybackAck) -> None:
    """Acknowledgements are evidence only and must match current generations."""
    active = rt.active
    track = active.segments.get(ack.identity.segment_id) if active else None
    stale = track is None or not rt.fence.accepts_ack(ack.identity)
    if active is None or track is None or stale:
        rt.ignored_acks += 1
        return
    if ack.ack is PlaybackAckKind.STARTED:
        track.playback_started = True
        await _playback_event(rt, active, track, EventType.PLAYBACK_STARTED)
    elif ack.ack is PlaybackAckKind.COMPLETED and track.status is SegmentStatus.PLAYING:
        track.status = SegmentStatus.DELIVERED
        active.turn = active.turn.record_spoken(track.segment.text, SpokenTextAccuracy.CONFIRMED)
        await _playback_event(rt, active, track, EventType.PLAYBACK_COMPLETED)
        await maybe_complete(rt)
    elif ack.ack is PlaybackAckKind.FAILED:
        track.status = SegmentStatus.FAILED
        await _playback_event(rt, active, track, EventType.PLAYBACK_FAILED)
        await maybe_complete(rt)


async def _playback_event(
    rt: SessionRuntime, active: ActiveTurnState, track: SegmentTrack, event_type: EventType
) -> None:
    await rt.emit(
        event_type,
        turn_id=active.turn.turn_id,
        component="playback",
        payload={"segment_sequence": track.segment.sequence},
    )
