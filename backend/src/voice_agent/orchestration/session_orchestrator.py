"""Session orchestrator: the single writer of user-visible session state (docs/05 §1, §5-§6).

Supporting tasks submit bounded commands; this command loop alone changes
turn/session state, activity, history, and cancellation generations. It
depends on ports only; adapters are injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

from voice_agent.contracts.enums import (
    DisconnectReason,
    OperationComponent,
    OperationStatus,
    SessionStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.stt import (
    SttEvent,
    SttFailed,
    SttFinalSegment,
    SttTurnFinalized,
    SttUsage,
)
from voice_agent.contracts.transport import PlaybackAck, RealtimeTopic
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.session import VoiceSession
from voice_agent.orchestration import delivery_flow, pipeline, turn_flow, turn_lifecycle
from voice_agent.orchestration.commands import (
    AudioReceived,
    ClientEventReceived,
    Command,
    InboundAudioOverflow,
    InputEnded,
    LateResultDiscarded,
    PlaybackPublished,
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
from voice_agent.orchestration.finalization import finalize
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.orchestration.interruption import accept_interruption
from voice_agent.orchestration.runtime import SessionPorts, SessionReport, SessionSettings
from voice_agent.orchestration.state import SessionRuntime
from voice_agent.turn_management.turn_manager import (
    AcceptInterruption,
    CommitEndpoint,
    InterruptionCandidateDetected,
    OpenTurn,
    SuppressFalseInterruption,
    TurnDecision,
    on_speech,
    on_tick,
)


class SessionOrchestrator:
    def __init__(
        self, *, session: VoiceSession, ports: SessionPorts, settings: SessionSettings
    ) -> None:
        if session.status is not SessionStatus.CONNECTING:
            raise ValueError("the orchestrator activates a session that is connecting")
        self._rt = SessionRuntime(session=session, ports=ports, settings=settings)

    @property
    def fence(self) -> GenerationFence:
        return self._rt.fence

    @property
    def session(self) -> VoiceSession:
        return self._rt.session

    @property
    def report(self) -> SessionReport:
        rt = self._rt
        return SessionReport(
            turns=tuple(rt.finished_turns),
            history=tuple(rt.history),
            cost=rt.cost,
            late_discards=dict(rt.late_discards),
            ignored_acks=rt.ignored_acks,
        )

    async def run(self) -> VoiceSession:
        """Activate, run the command loop until input ends, then finalize once."""
        reason, failed = DisconnectReason.UNKNOWN, True
        try:
            await self._activate()
            self._start_tasks()
            reason, failed = await self._command_loop()
        finally:
            await finalize(self._rt, reason, failed=failed)
        return self._rt.session

    async def finalize(
        self, reason: DisconnectReason = DisconnectReason.USER_ENDED, *, failed: bool = False
    ) -> VoiceSession:
        """Idempotent shared end path; repeated calls return the existing outcome."""
        return await finalize(self._rt, reason, failed=failed)

    async def _activate(self) -> None:
        rt = self._rt
        await rt.ports.stt.start(rt.settings.stt_config)
        await rt.ports.tts.open_session(rt.settings.voice)
        identity = rt.settings.stt_identity
        stream = ProviderOperation(
            operation_id=rt.new_id(),
            logical_request_id=rt.new_id(),
            session_id=rt.session.session_id,
            component=OperationComponent.STT,
            operation_type="transcribe_stream",
            provider=identity.provider,
            model=identity.model,
            worker_generation=rt.settings.worker_generation,
        ).transition_to(OperationStatus.STARTED)
        await rt.save_operation(stream)
        rt.stt_operation_id = stream.operation_id
        rt.fence.register_operation(stream.operation_id, session_scoped=True)
        await rt.save_session(rt.session.transition_to(SessionStatus.ACTIVE))
        await rt.emit(
            EventType.STT_STREAM_STARTED, operation_id=stream.operation_id, component="stt"
        )
        await rt.emit(
            EventType.SESSION_ACTIVE,
            payload={"agent_activity_state": "listening"},
            browser_topic=RealtimeTopic.STATE,
        )

    def _spawn(self, name: str, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(pipeline.guard(self._rt, name, work), name=name)
        self._rt.tasks.append(task)

    def _start_tasks(self) -> None:
        rt = self._rt
        self._spawn("transport_audio", pipeline.read_transport_audio(rt))
        self._spawn("inbound_audio", pipeline.forward_inbound_audio(rt))
        self._spawn("stt_events", pipeline.read_stt_events(rt))
        self._spawn("client_events", pipeline.read_client_events(rt))
        self._spawn("tts", pipeline.synthesize_segments(rt))
        self._spawn("playback", pipeline.publish_playback(rt))

    async def _command_loop(self) -> tuple[DisconnectReason, bool]:
        rt = self._rt
        while True:
            command = await rt.inbox.get()
            if isinstance(command, InputEnded):
                return DisconnectReason.USER_ENDED, False
            if isinstance(command, WorkerCrashed):
                await rt.record_failure(self._failure(ErrorType.INTERNAL_ERROR, command.task_name))
                return DisconnectReason.UNKNOWN, True
            await self._dispatch(command)

    async def _dispatch(self, command: Command) -> None:
        rt = self._rt
        match command:
            case AudioReceived(frame=frame):
                await self._on_audio(frame)
            case SttEventReceived(event=event):
                await self._on_stt(event)
            case ResponseTextReceived():
                await turn_flow.on_response_text(rt, command)
            case SegmentQueued():
                await turn_flow.on_segment_queued(rt, command)
            case SegmentRejected():
                await turn_flow.on_segment_rejected(rt, command)
            case ResponseTailDiscarded():
                rt.count_late("discarded_tail")
            case ResponseFinished():
                await turn_flow.on_response_finished(rt, command)
            case TtsStarted():
                await delivery_flow.on_tts_started(rt, command)
            case TtsFinished():
                await delivery_flow.on_tts_finished(rt, command)
            case PlaybackPublished():
                await delivery_flow.on_playback_published(rt, command)
            case ClientEventReceived(event=PlaybackAck() as ack):
                await delivery_flow.on_playback_ack(rt, ack)
            case ClientEventReceived():
                pass  # readiness, mic state, and latency samples are evidence only
            case LateResultDiscarded(source=source):
                rt.count_late(source)
            case InboundAudioOverflow():
                await self._on_overflow()

    async def _on_audio(self, frame: Any) -> None:
        rt = self._rt
        policy = rt.settings.turn_handling
        stamp = rt.fence.stamp(turn_id=rt.fence.active_turn, operation_id=rt.stt_operation_id)
        await rt.ports.stt.write_audio(frame, stamp)
        for event in rt.ports.speech_activity.process(frame):
            rt.turn_taking, decisions = on_speech(rt.turn_taking, event, policy)
            await self._apply(decisions)
        rt.turn_taking, decisions = on_tick(rt.turn_taking, frame.ends_at_ms, policy)
        await self._apply(decisions)

    async def _apply(self, decisions: Sequence[TurnDecision]) -> None:
        rt = self._rt
        for decision in decisions:
            match decision:
                case OpenTurn(speech_started_at_ms=started):
                    await turn_flow.open_turn(rt, started)
                case CommitEndpoint(last_speech_at_ms=last, committed_at_ms=committed):
                    await turn_flow.commit_endpoint(rt, last, committed)
                case InterruptionCandidateDetected():
                    await self._on_candidate(suppressed=False)
                case SuppressFalseInterruption():
                    await self._on_candidate(suppressed=True)
                case AcceptInterruption():
                    await accept_interruption(rt, decision)

    async def _on_candidate(self, *, suppressed: bool) -> None:
        rt = self._rt
        active = rt.active
        if active is None:
            return
        if suppressed:
            active.turn = active.turn.record_false_interruption()
            event_type = EventType.TURN_FALSE_INTERRUPTION_SUPPRESSED
        else:
            active.turn = active.turn.record_interruption_candidate()
            event_type = EventType.TURN_INTERRUPTION_DETECTED
        await rt.emit(event_type, turn_id=active.turn.turn_id, component="turn_management")

    async def _on_stt(self, event: SttEvent) -> None:
        rt = self._rt
        if isinstance(event, SttUsage):
            rt.stt_usage.append(event.usage)
        elif isinstance(event, SttTurnFinalized):
            await turn_flow.on_transcript(rt, event)
        elif isinstance(event, SttFinalSegment):
            if turn_lifecycle.current_turn(rt, event.stamp) is None:
                rt.count_late("stt_segment")
                return
            await rt.emit(
                EventType.STT_FINAL,
                turn_id=event.stamp.turn_id,
                operation_id=rt.stt_operation_id,
                component="stt",
                payload={"character_count": len(event.text)},
            )
        elif isinstance(event, SttFailed):
            await turn_lifecycle.fail_turn(rt, event.failure)

    def _failure(self, error_type: ErrorType, detail: str) -> NormalizedFailure:
        rt = self._rt
        return NormalizedFailure(
            component=ErrorComponent.ORCHESTRATOR,
            error_type=error_type,
            safe_message=f"{error_type.value}: {detail}",
            retryable=False,
            session_id=rt.session.session_id,
            turn_id=rt.active.turn.turn_id if rt.active else None,
            occurred_at=rt.ports.clock.utc_now(),
            user_affected=True,
        )

    async def _on_overflow(self) -> None:
        """Inbound audio overflow is ``realtime_overload`` evidence (docs/05 §9)."""
        rt = self._rt
        failure = self._failure(ErrorType.REALTIME_OVERLOAD, "inbound audio queue full")
        await rt.record_failure(failure)
        await rt.emit(
            EventType.ERROR_UNRECOVERABLE,
            turn_id=failure.turn_id,
            payload={"error_type": failure.error_type.value, "category": failure.category.value},
        )
        await turn_lifecycle.fail_turn(rt, failure, record=False)
