"""STT check session: local Silero VAD + Turn Manager + Deepgram, with no LLM or TTS (WP7).

Authority (docs/05 §11-§12, docs/07 §10):

- the local speech-activity detector alone reports speech start/stop;
- the Turn Manager opens turns and commits the endpoint at *last speech +
  endpoint deadline* (700 ms initially; the ~550 ms VAD silence window is not
  added on top), then asks STT to finalize that turn's audio window;
- provider speech/endpoint signals stay inside the adapter as counters;
- partial transcripts are lossy UI hints only; the turn-final transcript is
  sanitized and saved durably, and only then published as final and offered
  to the generation gate (which, in this check, authorizes nothing).

Microphone frames are fanned out twice by the transport (one 16 kHz copy per
consumer): the detector loop and the STT writer. Audio is never stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import JsonValue

from voice_agent.agent_worker.realtime_publisher import RealtimePublisher, sanitize_transcript
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.enums import AgentActivityState, InputDisposition
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.contracts.failures import NormalizedFailureError
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent
from voice_agent.contracts.stt import (
    AudioWindow,
    SttEvent,
    SttFailed,
    SttPartial,
    SttStreamClosed,
    SttStreamConfig,
    SttStreamStarted,
    SttTurnFinalized,
    SttWarning,
)
from voice_agent.contracts.transport import ClientReady
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.ports.stt import STTPort
from voice_agent.ports.transport import AgentAudioActivityProbe, SessionTransportPort
from voice_agent.response_segmentation.language import classify_turn_language
from voice_agent.turn_management.turn_manager import (
    AcceptInterruption,
    CommitEndpoint,
    OpenTurn,
    SuppressFalseInterruption,
    TurnDecision,
    TurnPhase,
    TurnTakingState,
    agent_response_finished,
    on_speech,
    on_tick,
)

CLOSE_TIMEOUT_S: Final = 5.0
TRANSCRIPT_GRACE_MS: Final = 2000
# Frames normally drive the endpoint tick; the timer only covers a stalled microphone.
ENDPOINT_TIMER_SLACK_S: Final = 0.04
_LOGGER = logging.getLogger("voice_agent.agent_worker.stt")


class GenerationGate(Protocol):
    async def authorize(self, turn: ConversationTurn) -> None:
        """Called only with a durably saved, accepted transcript turn."""
        ...


@dataclass
class NoGenerationGate:
    """The STT check has no conversation engine: nothing is ever generated."""

    offered: list[str] = field(default_factory=list)

    async def authorize(self, turn: ConversationTurn) -> None:
        self.offered.append(turn.turn_id)


@dataclass
class _Turn:
    turn: ConversationTurn
    speech_started_at_ms: int
    committed_at_ms: int | None = None
    committed_mono_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SttCheckSetup:
    session_id: str
    worker_generation: int
    policy: TurnHandlingPolicy
    stt_config: SttStreamConfig
    prefix_padding_ms: int = 500


class SttCheck:
    def __init__(
        self,
        transport: SessionTransportPort,
        setup: SttCheckSetup,
        *,
        stt: STTPort,
        detector: SpeechActivityPort,
        evidence: SttEvidence,
        publisher: RealtimePublisher,
        clock: Clock,
        ids: IdGenerator,
        gate: GenerationGate | None = None,
    ) -> None:
        self._transport = transport
        self._setup = setup
        self._policy = setup.policy
        self._stt = stt
        self._detector = detector
        self._evidence = evidence
        self._publisher = publisher
        self._clock = clock
        self._ids = ids
        self.gate = gate or NoGenerationGate()
        self._fence = GenerationFence(
            session_id=setup.session_id, worker_generation=setup.worker_generation
        )
        self._state = TurnTakingState()
        self._open: _Turn | None = None
        self._awaiting: dict[str, _Turn] = {}
        self._final: list[ConversationTurn] = []
        self._sequence = 0
        self._window_floor_ms = 0
        self._stt_operation: str | None = None
        self._stt_available = False
        self._endpoint_timer: asyncio.Task[None] | None = None
        self.counters: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    # ----------------------------------------------------------------- run --
    async def run(self) -> None:
        """Run until cancelled by the session runner; always closes STT and settles evidence."""
        try:
            await self._start_stt()
            await self._publisher.publish_state(AgentActivityState.LISTENING, force=True)
            async with asyncio.TaskGroup() as group:
                group.create_task(self._consume_stt(self._stt.events()))
                group.create_task(self._vad_loop())
                group.create_task(self._client_loop())
                if self._stt_available:
                    group.create_task(self._stt_writer())
        finally:
            await self._shutdown()

    async def _start_stt(self) -> None:
        try:
            await self._stt.start(self._setup.stt_config)
        except NormalizedFailureError as failed:
            _LOGGER.warning(
                "stt.start_failed", extra={"safe_fields": {"error": failed.failure.error_type}}
            )
            await self._evidence.event(
                EventType.STT_FAILED,
                payload={"error_type": failed.failure.error_type.value, "phase": "start"},
                severity=EventSeverity.ERROR,
            )
            await self._publisher.publish_error(
                "stt_unavailable", "Speech recognition is unavailable.", retryable=False
            )
            await self._publisher.publish_state(AgentActivityState.ERROR, force=True)
            return
        self._stt_available = True

    async def _stt_writer(self) -> None:
        stamp = self._fence.stamp()
        async for frame in self._transport.audio_frames():
            await self._stt.write_audio(frame, stamp)

    async def _client_loop(self) -> None:
        async for event in self._transport.client_events():
            if isinstance(event, ClientReady) and self._publisher.state is not None:
                await self._publisher.publish_state(self._publisher.state, force=True)

    # ----------------------------------------------------------- speech --
    async def _vad_loop(self) -> None:
        probe = self._transport if isinstance(self._transport, AgentAudioActivityProbe) else None
        async for frame in self._transport.audio_frames():
            # Raises the activation threshold while agent audio plays (0.7 vs 0.5).
            self._detector.set_playback_active(bool(probe and probe.agent_audio_active))
            for activity in self._detector.process(frame):
                await self._on_activity(activity)
            await self._tick(frame.ends_at_ms)

    async def _on_activity(self, activity: SpeechActivityEvent) -> None:
        self._state, decisions = on_speech(self._state, activity, self._policy)
        deadline = self._state.endpoint_deadline_ms
        if self._state.phase is TurnPhase.AWAITING_ENDPOINT and deadline is not None:
            self._arm_endpoint_timer(deadline, activity.at_ms)
        await self._apply(decisions)

    def _arm_endpoint_timer(self, deadline_ms: int, now_ms: int) -> None:
        """Commit on time even if microphone frames stop arriving (e.g. mute)."""
        if self._endpoint_timer is not None:
            self._endpoint_timer.cancel()
        remaining_s = max(deadline_ms - now_ms, 0) / 1000 + ENDPOINT_TIMER_SLACK_S
        self._endpoint_timer = asyncio.create_task(self._endpoint_after(remaining_s, deadline_ms))

    async def _endpoint_after(self, delay_s: float, deadline_ms: int) -> None:
        await asyncio.sleep(delay_s)
        if (
            self._state.phase is TurnPhase.AWAITING_ENDPOINT
            and self._state.endpoint_deadline_ms == deadline_ms
        ):
            self._count("endpoint_timer_commits")
            await self._tick(deadline_ms)

    async def _tick(self, now_ms: int) -> None:
        self._state, decisions = on_tick(self._state, now_ms, self._policy)
        await self._apply(decisions)
        await self._expire_awaiting()

    async def _apply(self, decisions: list[TurnDecision]) -> None:
        for decision in decisions:
            if isinstance(decision, OpenTurn):
                await self._open_turn(decision.speech_started_at_ms)
            elif isinstance(decision, CommitEndpoint):
                await self._commit(decision)
            elif isinstance(decision, SuppressFalseInterruption):
                self._count("false_interruptions_suppressed")
            elif isinstance(decision, AcceptInterruption):
                # No agent output exists in the STT check; the new turn opens next.
                self._count("interruptions_accepted")

    async def _open_turn(self, speech_started_at_ms: int) -> None:
        self._sequence += 1
        turn = ConversationTurn(
            turn_id=self._ids.new_id(),
            session_id=self._setup.session_id,
            sequence_number=self._sequence,
        )
        self._open = _Turn(turn=turn, speech_started_at_ms=speech_started_at_ms)
        self._fence.activate_turn(turn.turn_id)
        await self._evidence.save_turn(turn)
        await self._evidence.event(
            EventType.USER_SPEECH_STARTED,
            turn_id=turn.turn_id,
            payload={"speech_started_at_ms": speech_started_at_ms},
        )
        await self._evidence.event(EventType.TURN_OPENED, turn_id=turn.turn_id)

    async def _commit(self, decision: CommitEndpoint) -> None:
        active, self._open = self._open, None
        if active is None:
            return
        start = max(active.speech_started_at_ms - self._setup.prefix_padding_ms, 0)
        window = AudioWindow(
            start_ms=max(start, self._window_floor_ms), end_ms=decision.committed_at_ms
        )
        self._window_floor_ms = decision.committed_at_ms
        active.committed_at_ms = decision.committed_at_ms
        active.committed_mono_ms = self._clock.monotonic_ms()
        self._awaiting[active.turn.turn_id] = active
        await self._publisher.publish_state(AgentActivityState.TRANSCRIBING)
        await self._evidence.event(
            EventType.USER_SPEECH_ENDED,
            turn_id=active.turn.turn_id,
            payload={
                "last_speech_at_ms": decision.last_speech_at_ms,
                "committed_at_ms": decision.committed_at_ms,
                "endpoint_delay_ms": decision.committed_at_ms - decision.last_speech_at_ms,
            },
        )
        if not self._stt_available:
            await self._reject(active, InputDisposition.UNUSABLE, "stt_unavailable")
            return
        stamp = self._fence.stamp(turn_id=active.turn.turn_id, operation_id=self._stt_operation)
        await self._stt.finalize_turn(stamp, window)

    async def _expire_awaiting(self) -> None:
        """Safety net: a turn with no STT result well past the finalize timeout is closed."""
        limit = self._policy.stt_finalize_timeout_ms + TRANSCRIPT_GRACE_MS
        now = self._clock.monotonic_ms()
        for waiting in list(self._awaiting.values()):
            if waiting.committed_mono_ms is not None and now - waiting.committed_mono_ms > limit:
                self._awaiting.pop(waiting.turn.turn_id, None)
                await self._reject(waiting, InputDisposition.TIMED_OUT, "transcript_timeout")

    # ---------------------------------------------------------------- STT --
    async def _consume_stt(self, events: AsyncIterator[SttEvent]) -> None:
        async for event in events:
            await self._on_stt(event)

    async def _on_stt(self, event: SttEvent) -> None:
        if isinstance(event, SttPartial):
            await self._on_partial(event)
        elif isinstance(event, SttTurnFinalized):
            await self._on_transcript(event)
        elif isinstance(event, SttFailed):
            await self._on_failed(event)
        elif isinstance(event, SttStreamStarted):
            self._stt_operation = event.stamp.operation_id
            if event.stamp.operation_id is not None:
                self._fence.register_operation(event.stamp.operation_id, session_scoped=True)
            await self._evidence.stream_started(event)
        elif isinstance(event, SttStreamClosed):
            if event.stamp.operation_id is not None:
                self._fence.retire_operation(event.stamp.operation_id)
            await self._evidence.stream_closed(event)
        elif isinstance(event, SttWarning):
            await self._evidence.event(
                EventType.STT_WARNING,
                operation_id=event.stamp.operation_id,
                payload={"code": event.code},
                severity=EventSeverity.WARNING,
            )

    def _owns(self, stamp: GenerationStamp) -> _Turn | None:
        """Generation-fenced ownership check for a turn result (docs/07 §13)."""
        fence = self._fence
        if (
            stamp.session_id != fence.session_id
            or stamp.worker_generation != fence.worker_generation
            or stamp.cancellation_generation != fence.cancellation_generation
            or stamp.turn_id is None
        ):
            return None
        return self._awaiting.get(stamp.turn_id)

    async def _on_partial(self, event: SttPartial) -> None:
        if event.stamp.worker_generation != self._fence.worker_generation:
            self._count("late_partials")
            return
        target = self._open or next(reversed(self._awaiting.values()), None)
        if target is None:
            self._count("partials_without_turn")
            return
        await self._publisher.publish_transcript(
            event.text, turn_id=target.turn.turn_id, is_final=False
        )

    async def _on_transcript(self, event: SttTurnFinalized) -> None:
        waiting = self._owns(event.stamp)
        if waiting is None:
            self._count("late_transcripts")
            return
        self._awaiting.pop(waiting.turn.turn_id, None)
        text = sanitize_transcript(event.text, limit=10_000)
        await self._evidence.event(
            EventType.STT_TURN_FINALIZED,
            turn_id=waiting.turn.turn_id,
            operation_id=event.stamp.operation_id,
            payload=self._transcript_evidence(waiting, event, text),
        )
        if not text:
            await self._reject(waiting, InputDisposition.EMPTY, "empty_transcript")
            return
        await self._accept(waiting, text, event.language)

    def _transcript_evidence(
        self, waiting: _Turn, event: SttTurnFinalized, text: str
    ) -> dict[str, JsonValue]:
        now = self._clock.monotonic_ms()
        started = waiting.committed_mono_ms
        return {
            "usable": bool(text),
            "character_count": len(text),
            "word_count": len(text.split()),
            "finalization_timed_out": event.finalization_timed_out,
            "endpoint_to_final_ms": None if started is None else max(now - started, 0),
        }

    async def _accept(self, waiting: _Turn, text: str, detected: str | None) -> None:
        language = classify_turn_language(text, detected)
        accepted = waiting.turn.accept_transcript(text, language)
        if not await self._evidence.save_turn(accepted):
            # Not durable -> never authorized, never shown as final.
            await self._publisher.publish_error(
                "transcript_not_saved",
                "The transcript could not be saved.",
                retryable=True,
                turn_id=accepted.turn_id,
            )
            await self._finish_turn()
            return
        self._final.append(accepted)
        await self._publisher.publish_transcript(text, turn_id=accepted.turn_id, is_final=True)
        await self.gate.authorize(accepted)
        await self._finish_turn()

    async def _reject(self, waiting: _Turn, disposition: InputDisposition, reason: str) -> None:
        discarded = waiting.turn.reject_input(disposition).discard()
        await self._evidence.save_turn(discarded)
        await self._evidence.event(
            EventType.TURN_DISCARDED, turn_id=discarded.turn_id, payload={"reason": reason}
        )
        await self._finish_turn()

    async def _on_failed(self, event: SttFailed) -> None:
        if event.stamp.turn_id is None:
            self._stt_available = False
            await self._evidence.event(
                EventType.STT_FAILED,
                payload={"error_type": event.failure.error_type.value},
                severity=EventSeverity.ERROR,
            )
            await self._publisher.publish_error(
                "stt_unavailable", "Speech recognition is unavailable.", retryable=False
            )
            await self._publisher.publish_state(AgentActivityState.ERROR, force=True)
            return
        waiting = self._owns(event.stamp)
        if waiting is None:
            self._count("late_failures")
            return
        self._awaiting.pop(waiting.turn.turn_id, None)
        failed = waiting.turn.fail()
        await self._evidence.save_turn(failed)
        await self._evidence.event(
            EventType.TURN_FAILED,
            turn_id=failed.turn_id,
            payload={"error_type": event.failure.error_type.value},
            severity=EventSeverity.ERROR,
        )
        await self._publisher.publish_error(
            "stt_turn_failed",
            "That turn could not be transcribed.",
            retryable=True,
            turn_id=failed.turn_id,
        )
        await self._finish_turn()

    async def _finish_turn(self) -> None:
        if not self._awaiting and self._state.phase is TurnPhase.RESPONDING:
            self._state = agent_response_finished(self._state)
        if self._stt_available:
            await self._publisher.publish_state(AgentActivityState.LISTENING)

    # ------------------------------------------------------------ shutdown --
    async def _shutdown(self) -> None:
        if self._endpoint_timer is not None:
            self._endpoint_timer.cancel()
        with suppress(Exception):
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await self._stt.close()
                await self._consume_stt(self._stt.events())
        pending = [w.turn for w in (self._open, *self._awaiting.values()) if w is not None]
        for turn in (*pending, *self._final):
            await self._abandon(turn)
        await self._evidence.finish()

    async def _abandon(self, turn: ConversationTurn) -> None:
        if turn.is_terminal:
            return
        abandoned = turn.abandon()
        await self._evidence.save_turn(abandoned)
        await self._evidence.event(EventType.TURN_ABANDONED, turn_id=abandoned.turn_id)
