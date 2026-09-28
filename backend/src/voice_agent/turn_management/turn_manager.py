"""Turn Manager: the authority for turns, endpoints, and interruption acceptance.

A pure state machine (docs/03 §8, docs/05 §11-§12, §16). Local VAD reports
activity; this module decides:

- idle speech start opens a turn;
- the endpoint commits at ``last speech frame + endpoint deadline`` (the
  deadline is not added after the VAD silence window);
- while a response is in progress (thinking or speaking) speech is only an
  interruption *candidate*; it is accepted after >=250 ms of continuous
  speech, which also opens the next turn; anything shorter is suppressed as
  a false interruption and never creates a turn.

Time comes only from the audio timeline, so behaviour is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind


class TurnPhase(StrEnum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    AWAITING_ENDPOINT = "awaiting_endpoint"
    RESPONDING = "responding"


@dataclass(frozen=True, slots=True)
class OpenTurn:
    speech_started_at_ms: int


@dataclass(frozen=True, slots=True)
class CommitEndpoint:
    last_speech_at_ms: int
    committed_at_ms: int


@dataclass(frozen=True, slots=True)
class InterruptionCandidateDetected:
    candidate_started_at_ms: int


@dataclass(frozen=True, slots=True)
class AcceptInterruption:
    candidate_started_at_ms: int
    accepted_at_ms: int


@dataclass(frozen=True, slots=True)
class SuppressFalseInterruption:
    candidate_started_at_ms: int
    duration_ms: int


TurnDecision = (
    OpenTurn
    | CommitEndpoint
    | InterruptionCandidateDetected
    | AcceptInterruption
    | SuppressFalseInterruption
)


@dataclass(frozen=True, slots=True)
class TurnTakingState:
    phase: TurnPhase = TurnPhase.IDLE
    last_speech_at_ms: int | None = None
    endpoint_deadline_ms: int | None = None
    candidate_started_at_ms: int | None = None


Step = tuple[TurnTakingState, list[TurnDecision]]


def on_speech(
    state: TurnTakingState, event: SpeechActivityEvent, policy: TurnHandlingPolicy
) -> Step:
    if state.phase is TurnPhase.RESPONDING:
        return _responding(state, event, policy)
    if event.kind is SpeechActivityKind.SPEECH_STARTED:
        return _speech_started(state, event)
    if event.kind is SpeechActivityKind.SPEECH_STOPPED and state.phase is TurnPhase.USER_SPEAKING:
        deadline = event.last_speech_at_ms + policy.endpoint_deadline_ms
        stopped = replace(
            state,
            phase=TurnPhase.AWAITING_ENDPOINT,
            last_speech_at_ms=event.last_speech_at_ms,
            endpoint_deadline_ms=deadline,
        )
        return stopped, []
    return state, []


def _speech_started(state: TurnTakingState, event: SpeechActivityEvent) -> Step:
    if state.phase is TurnPhase.IDLE:
        opened = TurnTakingState(phase=TurnPhase.USER_SPEAKING)
        return opened, [OpenTurn(speech_started_at_ms=event.speech_started_at_ms)]
    if state.phase is TurnPhase.AWAITING_ENDPOINT:
        resumed = replace(state, phase=TurnPhase.USER_SPEAKING, endpoint_deadline_ms=None)
        return resumed, []
    return state, []


def _responding(
    state: TurnTakingState, event: SpeechActivityEvent, policy: TurnHandlingPolicy
) -> Step:
    if not policy.interruptions_enabled:
        return state, []
    candidate = state.candidate_started_at_ms
    if candidate is None:
        if event.kind is SpeechActivityKind.SPEECH_STOPPED or not event.is_speech:
            return state, []
        start = (
            event.speech_started_at_ms
            if event.kind is SpeechActivityKind.SPEECH_STARTED
            else event.at_ms - event.continuous_speech_ms
        )
        detected = replace(state, candidate_started_at_ms=start)
        decisions: list[TurnDecision] = [InterruptionCandidateDetected(start)]
        accepted_state, accepted = _maybe_accept(detected, event, policy)
        return accepted_state, decisions + accepted
    if event.kind is SpeechActivityKind.SPEECH_STOPPED or not event.is_speech:
        duration = max(event.last_speech_at_ms - candidate, 0)
        suppressed = replace(state, candidate_started_at_ms=None)
        return suppressed, [SuppressFalseInterruption(candidate, duration)]
    return _maybe_accept(state, event, policy)


def _maybe_accept(
    state: TurnTakingState, event: SpeechActivityEvent, policy: TurnHandlingPolicy
) -> Step:
    candidate = state.candidate_started_at_ms
    if candidate is None or event.continuous_speech_ms < policy.minimum_interruption_ms:
        return state, []
    opened = TurnTakingState(phase=TurnPhase.USER_SPEAKING)
    return opened, [
        AcceptInterruption(candidate_started_at_ms=candidate, accepted_at_ms=event.at_ms),
        OpenTurn(speech_started_at_ms=candidate),
    ]


def on_tick(state: TurnTakingState, now_ms: int, _policy: TurnHandlingPolicy) -> Step:
    deadline = state.endpoint_deadline_ms
    if state.phase is not TurnPhase.AWAITING_ENDPOINT or deadline is None or now_ms < deadline:
        return state, []
    last = state.last_speech_at_ms if state.last_speech_at_ms is not None else now_ms
    committed = TurnTakingState(phase=TurnPhase.RESPONDING)
    return committed, [CommitEndpoint(last_speech_at_ms=last, committed_at_ms=now_ms)]


def agent_response_finished(_state: TurnTakingState) -> TurnTakingState:
    """The current turn reached a terminal state; wait for the next utterance."""
    return TurnTakingState()
