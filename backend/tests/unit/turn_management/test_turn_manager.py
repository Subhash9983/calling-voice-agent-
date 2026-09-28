"""Turn Manager decisions: open, endpoint, interruption acceptance/suppression.

docs/03 §8, docs/05 §12, §16; docs/01 §7 (turn opens during playback only
after a confirmed >=250 ms candidate).
"""

from __future__ import annotations

from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind
from voice_agent.turn_management.turn_manager import (
    AcceptInterruption,
    CommitEndpoint,
    InterruptionCandidateDetected,
    OpenTurn,
    SuppressFalseInterruption,
    TurnPhase,
    TurnTakingState,
    agent_response_finished,
    on_speech,
    on_tick,
)

POLICY = TurnHandlingPolicy()


def _started(at: int) -> SpeechActivityEvent:
    return SpeechActivityEvent(
        kind=SpeechActivityKind.SPEECH_STARTED,
        at_ms=at,
        speech_started_at_ms=at - 60,
        last_speech_at_ms=at,
        continuous_speech_ms=60,
    )


def _progress(
    at: int, started: int, continuous: int, last: int | None = None
) -> SpeechActivityEvent:
    return SpeechActivityEvent(
        kind=SpeechActivityKind.SPEECH_PROGRESS,
        at_ms=at,
        speech_started_at_ms=started,
        last_speech_at_ms=at if last is None else last,
        continuous_speech_ms=continuous,
    )


def _stopped(at: int, started: int, last: int) -> SpeechActivityEvent:
    return SpeechActivityEvent(
        kind=SpeechActivityKind.SPEECH_STOPPED,
        at_ms=at,
        speech_started_at_ms=started,
        last_speech_at_ms=last,
    )


def test_speech_start_while_idle_opens_a_turn() -> None:
    state, decisions = on_speech(TurnTakingState(), _started(1060), POLICY)

    assert decisions == [OpenTurn(speech_started_at_ms=1000)]
    assert state.phase is TurnPhase.USER_SPEAKING


def test_endpoint_deadline_is_measured_from_last_speech_frame() -> None:
    state, _ = on_speech(TurnTakingState(), _started(1060), POLICY)
    state, decisions = on_speech(state, _stopped(at=2050, started=1000, last=1500), POLICY)
    assert decisions == []
    assert state.endpoint_deadline_ms == 1500 + POLICY.endpoint_deadline_ms

    state, early = on_tick(state, 2199, POLICY)
    state, due = on_tick(state, 2200, POLICY)

    assert early == []
    assert due == [CommitEndpoint(last_speech_at_ms=1500, committed_at_ms=2200)]
    assert state.phase is TurnPhase.RESPONDING


def test_resumed_speech_before_deadline_keeps_the_same_turn() -> None:
    state, _ = on_speech(TurnTakingState(), _started(1060), POLICY)
    state, _ = on_speech(state, _stopped(at=2050, started=1000, last=1500), POLICY)

    state, decisions = on_speech(state, _started(2100), POLICY)
    _, ticked = on_tick(state, 2300, POLICY)

    assert decisions == []
    assert ticked == []
    assert state.phase is TurnPhase.USER_SPEAKING


def _responding_state() -> TurnTakingState:
    state, _ = on_speech(TurnTakingState(), _started(1060), POLICY)
    state, _ = on_speech(state, _stopped(at=2050, started=1000, last=1500), POLICY)
    state, _ = on_tick(state, 2200, POLICY)
    return state


def test_candidate_during_response_does_not_open_a_turn_until_confirmed() -> None:
    state, decisions = on_speech(_responding_state(), _started(5060), POLICY)
    assert decisions == [InterruptionCandidateDetected(candidate_started_at_ms=5000)]

    state, below = on_speech(state, _progress(5240, started=5000, continuous=240), POLICY)
    assert below == []

    state, accepted = on_speech(state, _progress(5260, started=5000, continuous=260), POLICY)

    assert accepted == [
        AcceptInterruption(candidate_started_at_ms=5000, accepted_at_ms=5260),
        OpenTurn(speech_started_at_ms=5000),
    ]
    assert state.phase is TurnPhase.USER_SPEAKING
    assert state.candidate_started_at_ms is None


def test_short_candidate_is_suppressed_without_cancelling_the_response() -> None:
    state, _ = on_speech(_responding_state(), _started(5060), POLICY)

    state, decisions = on_speech(
        state, _progress(5200, started=5000, continuous=0, last=5140), POLICY
    )

    assert decisions == [SuppressFalseInterruption(candidate_started_at_ms=5000, duration_ms=140)]
    assert state.phase is TurnPhase.RESPONDING


def test_stop_before_confirmation_is_a_false_interruption() -> None:
    state, _ = on_speech(_responding_state(), _started(5060), POLICY)

    state, decisions = on_speech(state, _stopped(at=5700, started=5000, last=5150), POLICY)

    assert decisions == [SuppressFalseInterruption(candidate_started_at_ms=5000, duration_ms=150)]
    assert state.phase is TurnPhase.RESPONDING


def test_resumed_run_after_suppression_starts_a_new_candidate() -> None:
    state, _ = on_speech(_responding_state(), _started(5060), POLICY)
    state, _ = on_speech(state, _progress(5200, started=5000, continuous=0, last=5140), POLICY)

    state, decisions = on_speech(state, _progress(5320, started=5000, continuous=40), POLICY)

    assert decisions == [InterruptionCandidateDetected(candidate_started_at_ms=5280)]


def test_interruptions_disabled_ignores_candidates() -> None:
    policy = TurnHandlingPolicy(interruptions_enabled=False)
    state, _ = on_speech(TurnTakingState(), _started(1060), policy)
    state, _ = on_speech(state, _stopped(at=2050, started=1000, last=1500), policy)
    state, _ = on_tick(state, 2200, policy)

    _, decisions = on_speech(state, _started(5060), policy)

    assert decisions == []


def test_response_finished_returns_to_idle() -> None:
    state = agent_response_finished(_responding_state())

    assert state == TurnTakingState()
