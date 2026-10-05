"""The WP9 speaking gate under one session orchestrator (WP10; docs/05 §12-§16, docs/10 §4-§5).

:class:`OrchestratedGate` keeps every WP8/WP9 rule (one fenced generation per
accepted turn, retries, fallback, delivered-only history, four evidence
stages) and adds what the conversation orchestrator needs:

- :meth:`interrupt_response` runs the canonical interruption order for the
  active response: (2) advance the output fence, (3) cancel the LLM stream,
  then TTS (queued pieces are skipped, the active one cancelled), (4)
  ``AudioSource.clear_queue()`` - always, even with nothing speaking - (5)
  tell the browser, and returns acceptance-to-silence timing; (6) the
  interrupted turn is recorded with that timing when its run settles;
- :meth:`speak_template` speaks an application-owned phrase (the opening
  greeting, the clarification fallback, the time-limit notice) through the
  same segmentation/TTS/playback path with no LLM request;
- :meth:`record_candidate` counts interruption candidates and suppressed
  false interruptions on the responding turn;
- a done listener tells the orchestrator when a response (or phrase) settled.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import JsonValue

from voice_agent.agent_worker.llm_gate import ConversationSetup, _TurnRun, interruption_evidence
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_tracks import SpeechOutcome
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.agent_worker.tts_gate import SpeakingConversationGate, SpeechConfig
from voice_agent.contracts.enums import (
    AgentActivityState,
    InterruptionPhase,
    InterruptionReason,
    ResponseCompletionStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.domain.turn import ConversationTurn, InterruptionTiming
from voice_agent.orchestration.response_generation import GenerationResult, deliver_fallback
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.turn_management.fallbacks import FallbackTemplate

INTERRUPT_SETTLE_S: Final = 2.0
DoneListener = Callable[[str], Awaitable[None]]


async def _no_listener(_turn_id: str) -> None:
    return None


@dataclass(frozen=True, slots=True)
class InterruptRequest:
    reason: InterruptionReason
    accepted_mono_ms: int
    accepted_at: datetime
    # Capture-timeline acceptance frame -> decision delay (VAD/processing lag).
    detection_lag_ms: int | None = None


@dataclass(frozen=True, slots=True)
class InterruptReport:
    """What one accepted interruption stopped, and when audio went silent."""

    turn_id: str | None
    phase: InterruptionPhase | None
    audible: bool
    timing: InterruptionTiming


@dataclass(frozen=True, slots=True)
class _Pending:
    reason: InterruptionReason
    phase: InterruptionPhase
    settled: asyncio.Event
    timing: InterruptionTiming | None = None


_TEMPLATE_EVENTS: Final = {
    TurnStatus.COMPLETED: EventType.TURN_COMPLETED,
    TurnStatus.INTERRUPTED: EventType.TURN_INTERRUPTED,
    TurnStatus.DISCARDED: EventType.TURN_DISCARDED,
    TurnStatus.FAILED: EventType.TURN_FAILED,
}


class OrchestratedGate(SpeakingConversationGate):
    def __init__(
        self,
        setup: ConversationSetup,
        *,
        engine: ConversationEnginePort,
        speech: SpeechConfig,
        evidence: SttEvidence,
        publisher: RealtimePublisher,
        clock: Clock,
        ids: IdGenerator,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            setup,
            engine=engine,
            speech=speech,
            evidence=evidence,
            publisher=publisher,
            clock=clock,
            ids=ids,
            jitter=jitter,
        )
        self._pending: dict[str, _Pending] = {}
        self._templates: dict[str, FallbackTemplate] = {}
        self._done: DoneListener = _no_listener
        self._samples: dict[str, dict[str, JsonValue]] = {}

    def bind_done_listener(self, listener: DoneListener) -> None:
        self._done = listener

    @property
    def responding(self) -> bool:
        return self._active is not None

    @property
    def active_turn_id(self) -> str | None:
        return None if self._active is None else self._active.turn.turn_id

    # -------------------------------------------------------- candidates --
    def record_candidate(self, *, suppressed: bool) -> str | None:
        """Count a candidate / suppressed false interruption on the responding turn."""
        run = self._active
        if run is None or run.turn.is_terminal:
            return None
        run.turn = (
            run.turn.record_false_interruption()
            if suppressed
            else run.turn.record_interruption_candidate()
        )
        return run.turn.turn_id

    # --------------------------------------------------------- interrupt --
    def _phase_of(self, run: _TurnRun) -> InterruptionPhase:
        speech = self._speech.get(run.turn.turn_id)
        if run.turn.status is TurnStatus.AUDIO_STREAMING or (speech and speech.audible):
            return InterruptionPhase.SPEAKING
        if speech is not None and speech.tracks:
            return InterruptionPhase.SYNTHESIZING
        return InterruptionPhase.THINKING

    async def interrupt_response(self, request: InterruptRequest) -> InterruptReport:
        """Canonical interruption order (docs/05 §16); always clears the audio queue."""
        run = self._active
        phase = None if run is None else self._phase_of(run)
        speech = None if run is None else self._speech.get(run.turn.turn_id)
        audible = speech is not None and speech.audible
        pending = None
        if run is not None and phase is not None:
            pending = _Pending(request.reason, phase, asyncio.Event())
            self._pending[run.turn.turn_id] = pending
            await self._supersede_active()
        cleared_ms = None if speech is None else speech.cleared_at_ms
        if cleared_ms is None:
            await self._speech_config.transport.clear_playback()
            cleared_ms = self._clock.monotonic_ms()
        latency = max(cleared_ms - request.accepted_mono_ms, 0)
        timing = InterruptionTiming(
            accepted_at=request.accepted_at,
            playback_stopped_at=request.accepted_at + timedelta(milliseconds=latency),
            interruption_latency_ms=latency,
        )
        if run is not None:
            self._samples[run.turn.turn_id] = {
                "agent_audio_audible": audible,
                "detection_lag_ms": request.detection_lag_ms,
            }
        if run is not None and pending is not None:
            settled = _Pending(pending.reason, pending.phase, pending.settled, timing)
            self._pending[run.turn.turn_id] = settled
            pending.settled.set()
        turn_id = None if run is None else run.turn.turn_id
        return InterruptReport(turn_id=turn_id, phase=phase, audible=audible, timing=timing)

    async def _await_interrupt(self, turn_id: str) -> None:
        pending = self._pending.get(turn_id)
        if pending is not None:
            with suppress(TimeoutError):
                async with asyncio.timeout(INTERRUPT_SETTLE_S):
                    await pending.settled.wait()

    def _interruption_details(
        self, turn: ConversationTurn, *, superseded: bool
    ) -> tuple[InterruptionReason, InterruptionPhase, InterruptionTiming | None]:
        pending = self._pending.pop(turn.turn_id, None)
        if pending is None or self._closing:
            return super()._interruption_details(turn, superseded=superseded)
        return pending.reason, pending.phase, pending.timing

    def _turn_event_extra(self, turn: ConversationTurn) -> dict[str, JsonValue]:
        return self._samples.pop(turn.turn_id, {})

    # ------------------------------------------------------------ runs --
    async def _respond(self, run: _TurnRun) -> None:
        try:
            await super()._respond(run)
        finally:
            await self._done(run.turn.turn_id)

    async def _finish(self, run: _TurnRun, result: GenerationResult) -> None:
        await self._await_interrupt(run.turn.turn_id)
        await super()._finish(run, result)

    def _first_audio_callback(self, run: _TurnRun) -> Callable[[], Awaitable[None]]:
        if run.turn.turn_id not in self._templates:
            return super()._first_audio_callback(run)

        async def first_audio() -> None:
            if run.turn.is_terminal:
                return
            run.turn = run.turn.authorize_audio(fallback=True)
            await self._evidence.save_turn(run.turn)
            await self._publisher.publish_state(AgentActivityState.SPEAKING)

        return first_audio

    # -------------------------------------------------------- templates --
    async def speak_template(self, turn: ConversationTurn, template: FallbackTemplate) -> bool:
        """Speak an application-owned phrase for ``turn`` (no LLM); ``False`` if busy."""
        if self._closing or self._active is not None or turn.is_terminal:
            return False
        self._templates[turn.turn_id] = template
        self._fence.activate_turn(turn.turn_id)
        run = _TurnRun(turn=turn)
        self._active = run
        self._tasks[turn.turn_id] = asyncio.create_task(self._speak(run, template))
        return True

    async def _speak(self, run: _TurnRun, template: FallbackTemplate) -> None:
        turn_id = run.turn.turn_id
        try:
            await self.start()
            await deliver_fallback(
                template,
                fence=self._fence,
                turn_stamp=self._fence.stamp(turn_id=turn_id),
                language=run.turn.language,
                deliver=self._fallback_sink(run),
            )
            outcome = await self._drain(run)
            await self._await_interrupt(turn_id)
            await self._settle_template(run, template, outcome)
        except Exception:  # a crashed phrase never takes the session down
            await self._crashed(run)
        finally:
            self._templates.pop(turn_id, None)
            if self._active is run:
                self._active = None
                self._fence.deactivate_turn(turn_id)
            await self._done(turn_id)

    def _template_terminal(self, run: _TurnRun, outcome: SpeechOutcome) -> ConversationTurn:
        speech = self._speech.get(run.turn.turn_id)
        heard = any(s.is_fallback for s in outcome.spoken_segments)
        turn = self._record_output(run, run.turn)
        if (speech is not None and speech.cancelled) or self._closing:
            reason, phase, timing = self._interruption_details(turn, superseded=True)
            return turn.interrupt(reason=reason, phase=phase, timing=timing)
        if heard and turn.status is TurnStatus.AUDIO_STREAMING:
            return turn.complete(ResponseCompletionStatus.COMPLETED)
        return turn.discard() if turn.status is TurnStatus.OPEN else turn.fail()

    async def _settle_template(
        self, run: _TurnRun, template: FallbackTemplate, outcome: SpeechOutcome
    ) -> None:
        final = self._template_terminal(run, outcome)
        run.turn = final
        await self._evidence.save_turn(final)
        spoken = " ".join(s.text for s in outcome.spoken_segments if s.is_fallback)
        if final.status is not TurnStatus.DISCARDED:
            await self._publisher.publish_response_final(
                spoken,
                turn_id=final.turn_id,
                status=final.response_completion_status,
                fallback_template_id=template.template_id,
            )
        elif outcome.failure is not None:
            await self._publisher.publish_error(
                "speech_unavailable",
                "The spoken response could not be played.",
                retryable=True,
                turn_id=final.turn_id,
            )
        await self._evidence.event(
            _TEMPLATE_EVENTS[final.status],
            turn_id=final.turn_id,
            payload={
                "template_id": template.template_id,
                "reason_code": template.reason_code,
                "fallback_used": final.fallback_used,
                **interruption_evidence(final),
                **self._turn_event_extra(final),
            },
            severity=EventSeverity.WARNING if outcome.failure else EventSeverity.INFO,
        )
        if self._active is run and not self._closing:
            await self._publisher.publish_state(AgentActivityState.LISTENING)
        await self._release(run)
