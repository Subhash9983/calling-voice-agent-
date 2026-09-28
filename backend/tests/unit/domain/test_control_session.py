"""Control-plane session record rules (docs/02 §6, docs/04 §6, §7, §9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.domain.control_session import (
    MAX_JOIN_TOKEN_REQUESTS,
    ComponentSnapshot,
    ProviderSnapshot,
    SessionRecord,
    TerminationRequester,
    TransportBinding,
    request_fingerprint,
)
from voice_agent.domain.errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
    LifecycleStateError,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
SESSION_ID = "33333333-3333-4333-8333-333333333333"
CREATE_ID = "44444444-4444-4444-8444-444444444444"
END_ID = "55555555-5555-4555-8555-555555555555"
OTHER_ID = "66666666-6666-4666-8666-666666666666"
FINGERPRINT = request_fingerprint({"session_id": SESSION_ID})


def _component(provider: str) -> ComponentSnapshot:
    return ComponentSnapshot(provider=provider, model=None, adapter_version="mock-0.1.0")


def build_record(**overrides: object) -> SessionRecord:
    values: dict[str, object] = {
        "session_id": SESSION_ID,
        "client_request_id": CREATE_ID,
        "create_fingerprint": request_fingerprint({"agent_config_id": "x"}),
        "correlation_id": "77777777-7777-4777-8777-777777777777",
        "agent_id": "00000000-0000-4000-8000-00000000a9e1",
        "agent_config_id": "00000000-0000-4000-8000-00000000c0f1",
        "agent_config_version": 1,
        "config_checksum": "sha256:" + "a" * 64,
        "environment": "development",
        "language_mode": "auto",
        "provider_snapshot": ProviderSnapshot(
            transport=_component("mock_transport"),
            stt=_component("mock_stt"),
            conversation_engine=_component("mock_conversation"),
            tts=_component("mock_tts"),
        ),
        "maximum_session_ms": 1_800_000,
        "cost_currency": "USD",
        "cost_rate_card_version": "mock_rate_card_v1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SessionRecord.model_validate(values)


def connecting_record() -> SessionRecord:
    binding = TransportBinding(
        provider="mock_transport",
        external_room_id="room-1",
        external_session_id="dispatch-1",
        browser_participant_id="browser-1",
    )
    return build_record().bind_transport(binding, now=NOW)


def test_fingerprint_is_canonical_and_order_independent() -> None:
    first = request_fingerprint({"a": 1, "b": "x"})
    second = request_fingerprint({"b": "x", "a": 1})

    assert first == second
    assert first.startswith("sha256:")
    assert first != request_fingerprint({"a": 2, "b": "x"})


def test_new_record_defaults_are_safe() -> None:
    record = build_record()

    assert record.status is SessionStatus.CREATED
    assert record.recording_mode == "off"
    assert record.recording_status == "not_requested"
    assert record.initiator_type == "internal_tester"
    assert record.channel == "browser"
    assert record.session_mode == "interactive_test"
    assert record.join_token_requests == ()
    assert not record.is_terminal


def test_bind_transport_moves_to_connecting() -> None:
    record = connecting_record()

    assert record.status is SessionStatus.CONNECTING
    assert record.state_revision == 1
    assert record.connecting_at == NOW
    assert record.transport is not None


def test_fail_sets_terminal_reason_and_end_time() -> None:
    failed = build_record().fail(DisconnectReason.TRANSPORT_ERROR, now=NOW)

    assert failed.status is SessionStatus.FAILED
    assert failed.disconnect_reason is DisconnectReason.TRANSPORT_ERROR
    assert failed.ended_at == NOW
    assert failed.is_terminal
    with pytest.raises(InvalidTransitionError):
        failed.fail(DisconnectReason.UNKNOWN, now=NOW)


def test_request_end_records_termination_and_moves_to_ending() -> None:
    record = connecting_record()

    decision = record.request_end(
        client_request_id=END_ID,
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=NOW,
    )

    assert decision.accepted
    assert decision.record.status is SessionStatus.ENDING
    assert decision.record.state_revision == record.state_revision + 1
    request = decision.record.termination_request
    assert request is not None
    assert request.revision == 1
    assert request.requested_by is TerminationRequester.ANONYMOUS_USER
    assert decision.record.ending_at == NOW
    assert decision.record.disconnect_reason is None


def test_request_end_replay_and_later_request_reuse_existing_result() -> None:
    ending = (
        connecting_record()
        .request_end(
            client_request_id=END_ID,
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=NOW,
        )
        .record
    )

    replay = ending.request_end(
        client_request_id=END_ID,
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=NOW,
    )
    later = ending.request_end(
        client_request_id=OTHER_ID,
        reason=DisconnectReason.BROWSER_CLOSED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=NOW,
    )

    assert not replay.accepted
    assert replay.record is ending
    assert not later.accepted
    assert later.record.termination_request == ending.termination_request


def test_request_end_same_id_different_reason_conflicts() -> None:
    ending = (
        connecting_record()
        .request_end(
            client_request_id=END_ID,
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=NOW,
        )
        .record
    )

    with pytest.raises(IdempotencyConflictError):
        ending.request_end(
            client_request_id=END_ID,
            reason=DisconnectReason.BROWSER_CLOSED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=NOW,
        )


def test_request_end_on_terminal_session_never_reopens() -> None:
    failed = build_record().fail(DisconnectReason.TRANSPORT_ERROR, now=NOW)

    decision = failed.request_end(
        client_request_id=END_ID,
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=NOW,
    )

    assert not decision.accepted
    assert decision.record is failed
    assert decision.record.termination_request is None


def test_join_token_request_first_and_replay() -> None:
    record = connecting_record()

    first = record.record_join_token_request(
        client_request_id=END_ID, fingerprint=FINGERPRINT, now=NOW
    )
    later = NOW + timedelta(seconds=5)
    replay = first.record.record_join_token_request(
        client_request_id=END_ID, fingerprint=FINGERPRINT, now=later
    )

    assert not first.replay
    assert replay.replay
    (entry,) = replay.record.join_token_requests
    assert entry.issue_count == 2
    assert entry.first_requested_at == NOW
    assert entry.last_issued_at == later
    assert replay.record.state_revision == record.state_revision


def test_join_token_request_conflicting_fingerprint() -> None:
    record = connecting_record().record_join_token_request(
        client_request_id=END_ID, fingerprint=FINGERPRINT, now=NOW
    )

    with pytest.raises(IdempotencyConflictError):
        record.record.record_join_token_request(
            client_request_id=END_ID,
            fingerprint=request_fingerprint({"session_id": OTHER_ID}),
            now=NOW,
        )


def test_join_token_requests_are_bounded_to_most_recent() -> None:
    record = connecting_record()
    ids = [f"{index:08d}-0000-4000-8000-000000000000" for index in range(12)]
    for offset, client_id in enumerate(ids):
        record = record.record_join_token_request(
            client_request_id=client_id,
            fingerprint=FINGERPRINT,
            now=NOW + timedelta(seconds=offset),
        ).record

    kept = [entry.client_request_id for entry in record.join_token_requests]
    assert len(kept) == MAX_JOIN_TOKEN_REQUESTS
    assert kept == ids[-MAX_JOIN_TOKEN_REQUESTS:]


@pytest.mark.parametrize(
    "record_factory",
    [
        build_record,
        lambda: build_record().fail(DisconnectReason.UNKNOWN, now=NOW),
    ],
    ids=["not_dispatched", "terminal"],
)
def test_join_token_refused_outside_joinable_states(record_factory: object) -> None:
    record = record_factory()  # type: ignore[operator]

    with pytest.raises(LifecycleStateError):
        record.record_join_token_request(
            client_request_id=OTHER_ID, fingerprint=FINGERPRINT, now=NOW
        )


def test_join_token_allowed_while_ending() -> None:
    ending = (
        connecting_record()
        .request_end(
            client_request_id=END_ID,
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=NOW,
        )
        .record
    )

    decision = ending.record_join_token_request(
        client_request_id=OTHER_ID, fingerprint=FINGERPRINT, now=NOW
    )

    assert not decision.replay
    assert ending.can_issue_join_token(NOW)


def test_join_token_refused_after_maximum_duration() -> None:
    record = connecting_record()
    expired = NOW + timedelta(milliseconds=record.maximum_session_ms)

    assert record.maximum_duration_reached(expired)
    with pytest.raises(LifecycleStateError):
        record.record_join_token_request(
            client_request_id=OTHER_ID, fingerprint=FINGERPRINT, now=expired
        )


def test_record_rejects_unknown_fields_and_non_phase0_channel() -> None:
    with pytest.raises(ValueError, match="validation error"):
        build_record(join_token="synthetic-value")  # noqa: S106 - rejected field
    with pytest.raises(ValueError, match="validation error"):
        build_record(channel="phone")
