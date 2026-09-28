"""Session, turn, and operation state machines (docs/01 §5-§7, docs/02 §6-§8)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest

from voice_agent.contracts.enums import (
    AgentActivityState,
    DisconnectReason,
    FinishReason,
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
    ResponseCompletionStatus,
    ResponseLanguage,
    ResultDisposition,
    SessionStatus,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageReport
from voice_agent.costing.usage_normalization import tts_usage
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError
from voice_agent.domain.operation import OPERATION_TRANSITIONS, ProviderOperation
from voice_agent.domain.session import SESSION_TRANSITIONS, VoiceSession
from voice_agent.domain.turn import TURN_TRANSITIONS, ConversationTurn

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
OPERATION = "00000000-0000-4000-8000-000000000003"

ALLOWED_SESSION = {
    (SessionStatus.CREATED, SessionStatus.CONNECTING),
    (SessionStatus.CREATED, SessionStatus.ENDING),
    (SessionStatus.CREATED, SessionStatus.FAILED),
    (SessionStatus.CONNECTING, SessionStatus.ACTIVE),
    (SessionStatus.CONNECTING, SessionStatus.ENDING),
    (SessionStatus.CONNECTING, SessionStatus.FAILED),
    (SessionStatus.ACTIVE, SessionStatus.ENDING),
    (SessionStatus.ACTIVE, SessionStatus.FAILED),
    (SessionStatus.ENDING, SessionStatus.ENDED),
    (SessionStatus.ENDING, SessionStatus.FAILED),
}
T = TurnStatus
ALLOWED_TURN = {
    (T.OPEN, T.TRANSCRIPT_FINAL),
    (T.OPEN, T.AUDIO_STREAMING),
    (T.OPEN, T.INTERRUPTED),
    (T.OPEN, T.FAILED),
    (T.OPEN, T.ABANDONED),
    (T.OPEN, T.DISCARDED),
    (T.TRANSCRIPT_FINAL, T.RESPONSE_STREAMING),
    (T.TRANSCRIPT_FINAL, T.AUDIO_STREAMING),
    (T.TRANSCRIPT_FINAL, T.INTERRUPTED),
    (T.TRANSCRIPT_FINAL, T.FAILED),
    (T.TRANSCRIPT_FINAL, T.ABANDONED),
    (T.RESPONSE_STREAMING, T.AUDIO_STREAMING),
    (T.RESPONSE_STREAMING, T.COMPLETED),
    (T.RESPONSE_STREAMING, T.INTERRUPTED),
    (T.RESPONSE_STREAMING, T.FAILED),
    (T.RESPONSE_STREAMING, T.ABANDONED),
    (T.AUDIO_STREAMING, T.COMPLETED),
    (T.AUDIO_STREAMING, T.INTERRUPTED),
    (T.AUDIO_STREAMING, T.FAILED),
    (T.AUDIO_STREAMING, T.ABANDONED),
}


def _session(status: SessionStatus = SessionStatus.CREATED) -> VoiceSession:
    return VoiceSession(session_id=SESSION, correlation_id="c", status=status)


def _turn() -> ConversationTurn:
    return ConversationTurn(turn_id=TURN, session_id=SESSION, sequence_number=1)


# ---------------------------------------------------------------- session --


def test_session_transition_table_is_exactly_the_approved_set() -> None:
    table = {(src, dst) for src, targets in SESSION_TRANSITIONS.items() for dst in targets}
    assert table == ALLOWED_SESSION


@pytest.mark.parametrize(("source", "target"), list(product(SessionStatus, SessionStatus)))
def test_every_session_pair_is_either_allowed_or_rejected(
    source: SessionStatus, target: SessionStatus
) -> None:
    session = _session(source)
    if (source, target) in ALLOWED_SESSION:
        moved = session.transition_to(target)
        assert moved.status is target
        assert moved.state_revision == session.state_revision + 1
        assert session.status is source, "transitions return new objects"
    else:
        with pytest.raises(InvalidTransitionError):
            session.transition_to(target)


def test_activation_starts_listening_and_terminal_states_are_final() -> None:
    active = _session(SessionStatus.CONNECTING).transition_to(SessionStatus.ACTIVE)
    ended = active.transition_to(
        SessionStatus.ENDING, disconnect_reason=DisconnectReason.USER_ENDED
    )
    ended = ended.transition_to(SessionStatus.ENDED)

    assert active.agent_activity_state is AgentActivityState.LISTENING
    assert ended.is_terminal
    assert ended.disconnect_reason is DisconnectReason.USER_ENDED
    assert not ended.can_transition_to(SessionStatus.ACTIVE)


def test_activity_changes_only_while_active() -> None:
    active = _session(SessionStatus.CONNECTING).transition_to(SessionStatus.ACTIVE)

    speaking = active.with_activity(AgentActivityState.SPEAKING)

    assert speaking.agent_activity_state is AgentActivityState.SPEAKING
    assert speaking.with_activity(AgentActivityState.SPEAKING) is speaking
    with pytest.raises(DomainRuleError):
        _session().with_activity(AgentActivityState.LISTENING)


# ------------------------------------------------------------------- turn --


def test_turn_transition_table_is_exactly_the_approved_branching_set() -> None:
    table = {(src, dst) for src, targets in TURN_TRANSITIONS.items() for dst in targets}
    assert table == ALLOWED_TURN


def test_turn_opens_pending_and_not_started() -> None:
    turn = _turn()

    assert turn.status is TurnStatus.OPEN
    assert turn.input_disposition is InputDisposition.PENDING
    assert turn.response_completion_status is ResponseCompletionStatus.NOT_STARTED


def test_happy_path_open_final_streaming_audio_completed() -> None:
    turn = _turn().accept_transcript("namaste", ResponseLanguage.HINGLISH).start_response()
    turn = turn.record_generated("Hello. ").record_synthesized("Hello.")
    turn = turn.authorize_audio().authorize_audio()
    turn = turn.record_spoken("Hello.", SpokenTextAccuracy.CONFIRMED)
    turn = turn.record_finish_reason(FinishReason.COMPLETED).complete()

    assert turn.status is TurnStatus.COMPLETED
    assert turn.input_disposition is InputDisposition.ACCEPTED
    assert (turn.generated_text, turn.synthesized_text, turn.spoken_text) == (
        "Hello. ",
        "Hello.",
        "Hello.",
    )


def test_empty_transcript_cannot_enter_transcript_final() -> None:
    with pytest.raises(DomainRuleError, match="empty transcript"):
        _turn().accept_transcript("   ", None)


def test_noise_turn_is_discarded_not_failed() -> None:
    turn = _turn().reject_input(InputDisposition.EMPTY).discard()

    assert turn.status is TurnStatus.DISCARDED
    assert turn.is_terminal
    with pytest.raises(DomainRuleError):
        _turn().discard()
    with pytest.raises(DomainRuleError):
        _turn().reject_input(InputDisposition.ACCEPTED)


def test_clarification_fallback_goes_open_to_audio_to_completed() -> None:
    turn = _turn().reject_input(InputDisposition.UNUSABLE).authorize_audio(fallback=True).complete()

    assert turn.status is TurnStatus.COMPLETED
    assert turn.fallback_used


def test_skipping_response_streaming_requires_the_fallback() -> None:
    with pytest.raises(DomainRuleError, match="fallback"):
        _turn().accept_transcript("hi", None).authorize_audio()
    with pytest.raises(DomainRuleError, match="unusable"):
        _turn().authorize_audio(fallback=True)


def test_completion_without_audio_requires_no_speakable_output() -> None:
    streaming = _turn().accept_transcript("hi", None).start_response()

    with pytest.raises(DomainRuleError, match="no speakable"):
        streaming.complete()
    assert streaming.complete(no_speakable_output=True).status is TurnStatus.COMPLETED


@pytest.mark.parametrize(
    "completion",
    [ResponseCompletionStatus.TRUNCATED_PARTIAL, ResponseCompletionStatus.TRUNCATED_FALLBACK],
)
def test_truncation_is_only_a_completion_status(completion: ResponseCompletionStatus) -> None:
    turn = _turn().accept_transcript("hi", None).start_response().authorize_audio()

    completed = turn.complete(completion)

    assert completed.status is TurnStatus.COMPLETED
    assert completed.response_completion_status is completion
    with pytest.raises(DomainRuleError):
        turn.complete(ResponseCompletionStatus.INTERRUPTED)


def test_interruption_records_reason_phase_and_blocks_further_changes() -> None:
    turn = _turn().accept_transcript("hi", None).start_response().authorize_audio()
    turn = turn.record_interruption_candidate().record_false_interruption()
    turn = turn.record_interruption_candidate()

    interrupted = turn.interrupt(
        reason=InterruptionReason.USER_BARGE_IN, phase=InterruptionPhase.SPEAKING
    )

    assert interrupted.interruption.detected_count == 2
    assert interrupted.interruption.false_interruption_suppressed_count == 1
    assert interrupted.interruption.accepted
    assert interrupted.response_completion_status is ResponseCompletionStatus.INTERRUPTED
    with pytest.raises(DomainRuleError, match="terminal"):
        interrupted.record_generated("late")
    with pytest.raises(InvalidTransitionError):
        interrupted.complete()


@pytest.mark.parametrize("terminal", ["fail", "abandon"])
def test_failed_and_abandoned_are_terminal(terminal: str) -> None:
    turn = getattr(_turn(), terminal)()

    assert turn.is_terminal
    with pytest.raises(InvalidTransitionError):
        turn.start_response()


# -------------------------------------------------------------- operation --


def _operation() -> ProviderOperation:
    return ProviderOperation(
        operation_id=OPERATION,
        logical_request_id=TURN,
        session_id=SESSION,
        turn_id=TURN,
        component=OperationComponent.TTS,
        operation_type="synthesize_stream",
        provider="mock_tts",
        worker_generation=1,
    )


def test_operation_terminal_states_have_no_exits() -> None:
    for status in (OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.CANCELLED):
        assert OPERATION_TRANSITIONS[status] == frozenset()


def test_operation_success_and_late_usage() -> None:
    usage = tts_usage(synthesized_characters=12)
    done = _operation().transition_to(OperationStatus.STARTED).succeed(usage)

    assert done.result_disposition is ResultDisposition.USED
    assert done.with_late_usage(UsageReport.unavailable()).usage == UsageReport.unavailable()
    with pytest.raises(DomainRuleError):
        _operation().with_late_usage(usage)
    with pytest.raises(InvalidTransitionError):
        done.cancel()


def test_cancelled_operation_output_is_discarded_late() -> None:
    cancelled = _operation().transition_to(OperationStatus.STARTED).cancel()

    assert cancelled.result_disposition is ResultDisposition.DISCARDED_LATE
    assert cancelled.cancellation_requested
    assert cancelled.usage == UsageReport.unavailable()


def test_failed_operation_keeps_failure_and_usage() -> None:
    failure = NormalizedFailure(
        component=ErrorComponent.TTS,
        error_type=ErrorType.PROVIDER_UNAVAILABLE,
        safe_message="unavailable",
        retryable=True,
        session_id=SESSION,
        occurred_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    usage = tts_usage(synthesized_characters=3)

    failed = _operation().transition_to(OperationStatus.STARTED).fail(failure, usage)

    assert failed.failure == failure
    assert failed.usage == usage


def test_retry_attempt_gets_new_id_and_keeps_lineage() -> None:
    first = _operation()
    retry_id = "00000000-0000-4000-8000-000000000009"

    retry = first.next_attempt(retry_id)

    assert retry.operation_id == retry_id
    assert retry.logical_request_id == first.logical_request_id
    assert retry.turn_id == first.turn_id
    assert retry.attempt_number == 2
    assert retry.previous_attempt_operation_id == first.operation_id
    with pytest.raises(DomainRuleError, match="new operation ID"):
        first.next_attempt(first.operation_id)
