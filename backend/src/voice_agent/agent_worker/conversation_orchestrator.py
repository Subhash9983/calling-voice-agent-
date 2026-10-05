"""The authoritative real-path session orchestrator (WP10; docs/05 §5-§6, §11-§22; docs/06 §9, §13).

One per session. It extends the WP7 STT check (local Silero VAD + Turn
Manager + Deepgram, durable-before-authorized transcripts) with an
:class:`OrchestratedGate` (GPT-6 Luna -> Bulbul v3 -> agent audio) and owns:

- the turn lifecycle: listening -> endpoint commit -> final transcript ->
  generation -> speech -> playback -> listening. The Turn Manager stays in
  ``responding`` for the whole agent response (thinking *and* speaking), so
  user speech during it is only an interruption candidate;
- natural barge-in: the Silero threshold is 0.7 while agent audio plays
  (``vad.playback_activation_threshold``, via the transport's activity
  probe) and 0.5 while thinking; a candidate shorter than 250 ms is a
  suppressed false interruption that never cancels playback or opens a turn;
  >= 250 ms of continuous speech is accepted and runs the canonical order
  (fence -> LLM -> TTS -> ``clear_queue()`` -> browser -> evidence) through
  the gate, which returns acceptance-to-silence timing for the turn record;
- a turn committed but still awaiting its transcript when the user resumes
  speaking is interrupted before generation and its transcript is carried
  into the next turn (the user simply continued the same thought);
- the clarification fallback, once (no loop), after an accepted interruption
  whose next transcript is empty/unusable or when STT finalization timed out;
  stale audio is never resumed;
- the deterministic greeting exactly once, after the first ``client.ready``
  of a new session (never on a duplicate ready, a reconnect, or a
  worker-crash recovery);
- approved timeouts (silence -> ``idle`` state, maximum user turn, session
  idle -> ``idle_timeout`` end, the time-limit notice before the maximum
  duration); the 20 s reconnect window stays with the transport;
- browser absence: output is cancelled and new turns pause until reconnect.

There is no LiveKit ``AgentSession`` and no semantic/audio Turn Detector.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, Final

from pydantic import JsonValue

from voice_agent.agent_worker.conversation_gate import InterruptRequest, OrchestratedGate
from voice_agent.agent_worker.conversation_policy import ConversationTimeouts
from voice_agent.agent_worker.llm_gate import interruption_evidence
from voice_agent.agent_worker.ordered_writer import OrderedWriter
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher, sanitize_transcript
from voice_agent.agent_worker.session_runner import EndRequest
from voice_agent.agent_worker.stt_check import SttCheck, SttCheckSetup, _Turn
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.enums import (
    AgentActivityState,
    DisconnectReason,
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
)
from voice_agent.contracts.events import EventType
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind
from voice_agent.contracts.stt import SttTurnFinalized
from voice_agent.contracts.transport import (
    ClientReady,
    PlaybackAck,
    TransportEvent,
    TransportEventKind,
)
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.ports.stt import STTPort
from voice_agent.ports.transport import AgentAudioActivityProbe, SessionTransportPort
from voice_agent.turn_management.fallbacks import SESSION_TIME_LIMIT, UNCLEAR_INPUT
from voice_agent.turn_management.greeting import OPENING_GREETING
from voice_agent.turn_management.turn_manager import (
    AcceptInterruption,
    CommitEndpoint,
    InterruptionCandidateDetected,
    OpenTurn,
    SuppressFalseInterruption,
    TurnDecision,
    TurnPhase,
    TurnTakingState,
    agent_response_finished,
)

PAUSE_EVENTS: Final = frozenset({TransportEventKind.BROWSER_LEFT, TransportEventKind.RECONNECTING})
RESUME_EVENTS: Final = frozenset(
    {TransportEventKind.BROWSER_JOINED, TransportEventKind.RECONNECTED}
)
MS_PER_SECOND: Final = 1000
MAX_PLAUSIBLE_LAG_MS: Final = 10_000


def _no_end(_reason: DisconnectReason) -> None:
    return None


class ConversationOrchestrator(SttCheck):
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
        gate: OrchestratedGate,
        timeouts: ConversationTimeouts,
        request_end: EndRequest = _no_end,
        greeting_enabled: bool = True,
    ) -> None:
        super().__init__(
            transport,
            setup,
            stt=stt,
            detector=detector,
            evidence=evidence,
            publisher=publisher,
            clock=clock,
            ids=ids,
            gate=gate,
        )
        self._speaking = gate
        self._timeouts = timeouts
        self._request_end = request_end
        self._greeting_enabled = greeting_enabled
        self._greeting_attempted = False
        self._writer = OrderedWriter()
        self._side: set[asyncio.Task[None]] = set()
        self._paused = False
        self._blocked = False
        self._pending_barge = False
        self._barge_sources: list[str] = []
        self._after_barge: set[str] = set()
        self._superseded: dict[str, str | None] = {}
        self._carry: dict[str, list[str]] = {}
        self._timed_out: set[str] = set()
        self._unclear_pending = False
        self._open_mono_ms: int | None = None
        self._last_frame_ms = 0
        self._last_activity_ms = clock.monotonic_ms()
        self._idle_published = False
        gate.bind_done_listener(self._on_response_done)

    # ------------------------------------------------------------- tasks --
    def _extra_tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self._watch_timeouts()]

    def _mark_activity(self) -> None:
        self._last_activity_ms = self._clock.monotonic_ms()
        self._idle_published = False

    def _busy(self) -> bool:
        return self._speaking.responding or self._open is not None or bool(self._awaiting)

    def _playback_active(self) -> bool:
        probe = self._transport if isinstance(self._transport, AgentAudioActivityProbe) else None
        return bool(probe and probe.agent_audio_active)

    def _interrupt_request(self, reason: InterruptionReason) -> InterruptRequest:
        return InterruptRequest(
            reason=reason,
            accepted_mono_ms=self._clock.monotonic_ms(),
            accepted_at=self._clock.utc_now(),
        )

    # ------------------------------------------------------------ speech --
    async def _vad_loop(self) -> None:
        async for frame in self._transport.audio_frames():
            self._last_frame_ms = frame.ends_at_ms
            if self._paused:
                continue  # the browser is absent: no turns, no output (docs/05 §19)
            # 0.7 while agent audio plays, 0.5 otherwise (Decision 067).
            self._detector.set_playback_active(self._playback_active())
            for activity in self._detector.process(frame):
                await self._on_activity(activity)
            await self._tick(frame.ends_at_ms)

    async def _on_activity(self, activity: SpeechActivityEvent) -> None:
        if activity.is_speech:
            self._mark_activity()
        await super()._on_activity(self._resumed_speech(activity))

    def _resumed_speech(self, activity: SpeechActivityEvent) -> SpeechActivityEvent:
        """Speech already in progress when a response ends opens a turn, not nothing.

        The detector reports ``speech_started`` only once per speech run; if the
        agent finishes while the user is mid-run (a candidate still pending), the
        Turn Manager is back at ``idle`` and would ignore the rest of the run.
        """
        if (
            self._state.phase is not TurnPhase.IDLE
            or activity.kind is not SpeechActivityKind.SPEECH_PROGRESS
            or not activity.is_speech
        ):
            return activity
        self._count("resumed_speech_runs")
        start = max(activity.at_ms - activity.continuous_speech_ms, 0)
        return activity.model_copy(
            update={"kind": SpeechActivityKind.SPEECH_STARTED, "speech_started_at_ms": start}
        )

    async def _apply(self, decisions: list[TurnDecision]) -> None:
        for decision in decisions:
            if isinstance(decision, AcceptInterruption):
                await self._on_barge_in(decision)
            elif isinstance(decision, InterruptionCandidateDetected | SuppressFalseInterruption):
                self._on_candidate(decision)
            elif isinstance(decision, OpenTurn):
                await self._on_open(decision)
            elif isinstance(decision, CommitEndpoint):
                await self._commit(decision)

    def _on_candidate(
        self, decision: InterruptionCandidateDetected | SuppressFalseInterruption
    ) -> None:
        suppressed = isinstance(decision, SuppressFalseInterruption)
        turn_id = self._speaking.record_candidate(suppressed=suppressed)
        self._count("false_interruptions_suppressed" if suppressed else "interruption_candidates")
        payload: dict[str, JsonValue] = {
            "candidate_started_at_ms": decision.candidate_started_at_ms,
            "playback_active": self._playback_active(),
        }
        if isinstance(decision, SuppressFalseInterruption):
            payload["duration_ms"] = decision.duration_ms
        event_type = (
            EventType.TURN_FALSE_INTERRUPTION_SUPPRESSED
            if suppressed
            else EventType.TURN_INTERRUPTION_DETECTED
        )
        evidence = self._evidence
        self._writer.submit(lambda: evidence.event(event_type, turn_id=turn_id, payload=payload))

    async def _on_barge_in(self, decision: AcceptInterruption) -> None:
        if self._blocked:
            # The time-limit notice is not interruptible; the session is ending.
            self._state = TurnTakingState(phase=TurnPhase.RESPONDING)
            return
        self._count("interruptions_accepted")
        self._mark_activity()
        request = self._interrupt_request(InterruptionReason.USER_BARGE_IN)
        lag = request.accepted_mono_ms - decision.accepted_at_ms
        if 0 <= lag <= MAX_PLAUSIBLE_LAG_MS:  # capture and worker clocks share a timeline
            request = dataclasses.replace(request, detection_lag_ms=lag)
        superseded = list(self._awaiting.values())
        self._awaiting.clear()
        report = await self._speaking.interrupt_response(request)
        self._pending_barge = True
        self._barge_sources = [waiting.turn.turn_id for waiting in superseded]
        for waiting in superseded:
            self._superseded[waiting.turn.turn_id] = None
            turn = waiting.turn.interrupt(
                reason=InterruptionReason.USER_BARGE_IN,
                phase=InterruptionPhase.THINKING,
                timing=report.timing,
            )
            await self._evidence.save_turn(turn)
            await self._evidence.event(
                EventType.TURN_INTERRUPTED,
                turn_id=turn.turn_id,
                payload={"superseded_before_transcript": True, **interruption_evidence(turn)},
            )

    async def _on_open(self, decision: OpenTurn) -> None:
        if self._blocked or self._paused:
            self._state = TurnTakingState(
                phase=TurnPhase.RESPONDING if self._blocked else TurnPhase.IDLE
            )
            return
        await self._open_turn(decision.speech_started_at_ms)
        self._open_mono_ms = self._clock.monotonic_ms()
        opened = self._open
        if self._pending_barge and opened is not None:
            self._after_barge.add(opened.turn.turn_id)
            for source in self._barge_sources:
                self._superseded[source] = opened.turn.turn_id
        self._pending_barge = False
        self._barge_sources = []

    # --------------------------------------------------------- transcript --
    async def _on_transcript(self, event: SttTurnFinalized) -> None:
        turn_id = event.stamp.turn_id
        if turn_id is not None and turn_id in self._superseded:
            target = self._superseded.pop(turn_id)
            text = sanitize_transcript(event.text, limit=10_000)
            if text and target is not None:
                self._carry.setdefault(target, []).append(text)
                self._count("carried_transcripts")
            return
        waiting = self._owns(event.stamp)
        if waiting is not None:
            carried = self._carry.pop(waiting.turn.turn_id, [])
            text = sanitize_transcript(event.text, limit=10_000)
            if event.finalization_timed_out and not text and not carried:
                self._timed_out.add(waiting.turn.turn_id)
            if carried:
                merged = " ".join([*carried, text]).strip()
                event = event.model_copy(update={"text": merged})
        await super()._on_transcript(event)

    async def _accept(self, waiting: _Turn, text: str, detected: str | None) -> None:
        self._after_barge.discard(waiting.turn.turn_id)
        if self._blocked or self._paused:
            reason = "session_time_limit" if self._blocked else "browser_absent"
            await self._abandon_with(waiting.turn, reason)
            await self._finish_turn()
            return
        self._unclear_pending = False
        self._mark_activity()
        await super()._accept(waiting, text, detected)

    def _should_clarify(self, turn_id: str, disposition: InputDisposition, reason: str) -> bool:
        if reason == "stt_unavailable" or self._blocked or self._paused or self._unclear_pending:
            return False
        return turn_id in self._after_barge or disposition is InputDisposition.TIMED_OUT

    async def _reject(self, waiting: _Turn, disposition: InputDisposition, reason: str) -> None:
        turn_id = waiting.turn.turn_id
        if turn_id in self._timed_out:
            self._timed_out.discard(turn_id)
            disposition = InputDisposition.TIMED_OUT
        clarify = self._should_clarify(turn_id, disposition, reason)
        self._after_barge.discard(turn_id)
        if clarify:
            turn = waiting.turn.reject_input(disposition)
            await self._evidence.save_turn(turn)
            if await self._speaking.speak_template(turn, UNCLEAR_INPUT):
                self._unclear_pending = True
                self._count("clarifications")
                return
        await super()._reject(waiting, disposition, reason)

    async def _finish_turn(self, *, publish_listening: bool = True) -> None:
        if self._speaking.responding or self._awaiting:
            return  # the response (or another transcript) still owns the turn state
        await super()._finish_turn(publish_listening=publish_listening)

    async def _on_response_done(self, _turn_id: str) -> None:
        self._mark_activity()
        if self._busy() or self._state.phase is not TurnPhase.RESPONDING:
            return
        self._state = agent_response_finished(self._state)

    # ------------------------------------------------------- agent turns --
    async def _new_agent_turn(self, origin: str) -> ConversationTurn:
        """A turn for an application-owned phrase: no user input, no LLM request."""
        self._sequence += 1
        turn = ConversationTurn(
            turn_id=self._ids.new_id(),
            session_id=self._setup.session_id,
            sequence_number=self._sequence,
        ).reject_input(InputDisposition.EMPTY)
        await self._evidence.save_turn(turn)
        await self._evidence.event(
            EventType.TURN_OPENED, turn_id=turn.turn_id, payload={"origin": origin}
        )
        return turn

    async def _client_loop(self) -> None:
        async for event in self._transport.client_events():
            if isinstance(event, ClientReady):
                await self._on_client_ready()
            elif isinstance(event, PlaybackAck):
                await self._speaking.on_playback_ack(event)

    async def _on_client_ready(self) -> None:
        if self._publisher.state is not None:
            await self._publisher.publish_state(self._publisher.state, force=True)
        if self._greeting_attempted:
            self._count("duplicate_client_ready")
            return
        self._greeting_attempted = True
        if not self._greeting_enabled or self._blocked or self._paused:
            return
        if self._busy() or self._state.phase is not TurnPhase.IDLE:
            self._count("greeting_skipped")
            return
        turn = await self._new_agent_turn("greeting")
        self._state = TurnTakingState(phase=TurnPhase.RESPONDING)
        if await self._speaking.speak_template(turn, OPENING_GREETING):
            self._count("greetings")
            self._mark_activity()
            return
        self._state = TurnTakingState()
        await self._evidence.save_turn(turn.discard())

    # ---------------------------------------------------------- lifecycle --
    def on_transport_event(self, event: TransportEvent) -> None:
        """Runner lifecycle listener (sync): browser absence pauses, return resumes."""
        if event.kind not in PAUSE_EVENTS | RESUME_EVENTS:
            return
        task = asyncio.ensure_future(self._on_lifecycle(event.kind))
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    async def _on_lifecycle(self, kind: TransportEventKind) -> None:
        if kind in PAUSE_EVENTS:
            await self._pause()
        elif self._paused:
            self._paused = False
            self._detector.reset()
            self._mark_activity()
            await self._publisher.publish_state(AgentActivityState.LISTENING, force=True)

    async def _pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._count("browser_absences")
        await self._speaking.interrupt_response(
            self._interrupt_request(InterruptionReason.SYSTEM_CANCEL)
        )
        active, self._open = self._open, None
        if active is not None:
            await self._abandon_with(active.turn, "browser_absent")
        self._state = TurnTakingState()
        await self._publisher.publish_state(AgentActivityState.RECOVERING, force=True)

    async def _abandon_with(self, turn: ConversationTurn, reason: str) -> None:
        if turn.is_terminal:
            return
        abandoned = turn.abandon()
        await self._evidence.save_turn(abandoned)
        await self._evidence.event(
            EventType.TURN_ABANDONED, turn_id=abandoned.turn_id, payload={"reason": reason}
        )

    # ----------------------------------------------------------- timeouts --
    async def _watch_timeouts(self) -> None:
        while True:
            await asyncio.sleep(self._timeouts.tick_s)
            if await self._check_timeouts():
                return

    async def _check_timeouts(self) -> bool:
        """One watchdog pass; ``True`` once the session end has been requested."""
        if self._time_limit_due():
            await self._announce_time_limit()
            return True
        now = self._clock.monotonic_ms()
        opened = self._open_mono_ms
        overdue = opened is not None and now - opened >= self._timeouts.maximum_user_turn_ms
        if self._open is not None and overdue:
            await self._force_commit()
        if self._paused or self._busy():
            return False
        quiet = now - self._last_activity_ms
        if quiet >= self._timeouts.idle_session_ms:
            self._count("idle_timeouts")
            self._request_end(DisconnectReason.IDLE_TIMEOUT)
            return True
        if quiet >= self._timeouts.maximum_silence_ms and not self._idle_published:
            self._idle_published = True
            await self._publisher.publish_state(AgentActivityState.IDLE)
        return False

    def _time_limit_due(self) -> bool:
        deadline = self._timeouts.maximum_duration_deadline_at
        if deadline is None:
            return False
        remaining = (deadline - self._clock.utc_now()).total_seconds() * MS_PER_SECOND
        return remaining <= self._timeouts.time_limit_notice_ms

    async def _force_commit(self) -> None:
        """The approved maximum user-turn duration commits a still-open turn."""
        self._count("maximum_user_turn_commits")
        self._open_mono_ms = None
        last = self._state.last_speech_at_ms or self._last_frame_ms
        self._state = TurnTakingState(phase=TurnPhase.RESPONDING)
        committed = max(self._last_frame_ms, last)
        await self._commit(CommitEndpoint(last_speech_at_ms=last, committed_at_ms=committed))

    async def _announce_time_limit(self) -> None:
        """Speak the time-limit phrase once, then end as ``maximum_duration`` (REL-100)."""
        self._blocked = True
        self._count("time_limit_notices")
        await self._speaking.interrupt_response(
            self._interrupt_request(InterruptionReason.SYSTEM_CANCEL)
        )
        with suppress(TimeoutError):
            async with asyncio.timeout(self._timeouts.time_limit_notice_ms / MS_PER_SECOND):
                await self._speaking.wait_idle()
        active, self._open = self._open, None
        if active is not None:
            await self._abandon_with(active.turn, "session_time_limit")
        turn = await self._new_agent_turn("time_limit")
        self._state = TurnTakingState(phase=TurnPhase.RESPONDING)
        if await self._speaking.speak_template(turn, SESSION_TIME_LIMIT):
            with suppress(TimeoutError):
                async with asyncio.timeout(self._timeouts.time_limit_notice_ms / MS_PER_SECOND):
                    await self._speaking.wait_idle()
        self._request_end(DisconnectReason.MAXIMUM_DURATION)

    # ----------------------------------------------------------- shutdown --
    async def _shutdown(self) -> None:
        for task in list(self._side):
            task.cancel()
        await self._writer.close()
        await super()._shutdown()
