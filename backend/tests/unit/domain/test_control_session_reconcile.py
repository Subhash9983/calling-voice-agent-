"""Reconciler-facing session rules: worker presence and bounded completion (docs/05 §21)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.domain.test_control_session import END_ID, NOW, build_record

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.domain.control_session import (
    SessionRecord,
    TerminationRequester,
    TransportBinding,
)
from voice_agent.domain.errors import LifecycleStateError
from voice_agent.domain.session_reconcile import ReconcileAction, reconcile_action

BINDING = TransportBinding(
    provider="livekit",
    external_room_id="va-rd-room",
    external_session_id="AD_x",
    browser_participant_id="va-user-1",
    agent_participant_id="va-agent-1",
)


def _connecting(**overrides: object) -> SessionRecord:
    record = build_record(connect_deadline_at=NOW + timedelta(seconds=20), **overrides)
    return record.bind_transport(BINDING, now=NOW)


def _ending(reason: DisconnectReason, **overrides: object) -> SessionRecord:
    record = _connecting(**overrides)
    return record.request_end(
        client_request_id=END_ID,
        reason=reason,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=NOW,
    ).record


def test_binding_keeps_agent_identity_optional() -> None:
    legacy = TransportBinding(provider="mock", external_room_id="r", browser_participant_id="b")

    assert legacy.agent_participant_id is None
    assert BINDING.agent_participant_id == "va-agent-1"


def test_live_worker_requires_unexpired_lease() -> None:
    record = build_record()
    assert not record.has_live_worker(NOW)

    leased = build_record(worker_lease_expires_at=NOW + timedelta(seconds=5))
    assert leased.has_live_worker(NOW)
    assert not leased.has_live_worker(NOW + timedelta(seconds=5))


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (DisconnectReason.USER_ENDED, SessionStatus.ENDED),
        (DisconnectReason.BROWSER_CLOSED, SessionStatus.ENDED),
        (DisconnectReason.NETWORK_LOST, SessionStatus.ENDED),
        (DisconnectReason.IDLE_TIMEOUT, SessionStatus.ENDED),
        (DisconnectReason.MAXIMUM_DURATION, SessionStatus.ENDED),
        (DisconnectReason.TRANSPORT_ERROR, SessionStatus.FAILED),
        (DisconnectReason.PROVIDER_ERROR, SessionStatus.FAILED),
        (DisconnectReason.SERVER_SHUTDOWN, SessionStatus.FAILED),
        (DisconnectReason.UNKNOWN, SessionStatus.FAILED),
    ],
)
def test_finalize_end_maps_reason_to_terminal_state(
    reason: DisconnectReason, status: SessionStatus
) -> None:
    ending = _ending(reason)

    final = ending.finalize_end(now=NOW + timedelta(seconds=1))

    assert final.status is status
    assert final.disconnect_reason is reason
    assert final.ended_at == NOW + timedelta(seconds=1)
    assert final.termination_deadline_at is None
    assert final.next_reconcile_at is None


def test_finalize_end_requires_ending() -> None:
    with pytest.raises(LifecycleStateError):
        build_record().finalize_end(now=NOW)


def test_idle_or_live_sessions_need_no_action() -> None:
    record = _connecting()
    assert reconcile_action(record, NOW + timedelta(seconds=1)) is ReconcileAction.NONE


def test_connect_timeout_fails_before_active() -> None:
    record = _connecting()
    later = NOW + timedelta(seconds=21)

    assert reconcile_action(record, later) is ReconcileAction.FAIL_CONNECT_TIMEOUT


def test_ending_without_worker_finalizes() -> None:
    ending = _ending(DisconnectReason.USER_ENDED)

    assert reconcile_action(ending, NOW) is ReconcileAction.FINALIZE_END


def test_ending_with_live_worker_waits_until_deadline_and_lease_pass() -> None:
    lease = NOW + timedelta(seconds=60)
    ending = _ending(DisconnectReason.USER_ENDED, worker_lease_expires_at=lease)

    assert reconcile_action(ending, NOW + timedelta(seconds=11)) is ReconcileAction.NONE
    assert reconcile_action(ending, lease) is ReconcileAction.FINALIZE_END


def test_maximum_duration_requests_end() -> None:
    record = _connecting(maximum_session_ms=1000)

    assert (
        reconcile_action(record, NOW + timedelta(seconds=1))
        is ReconcileAction.REQUEST_MAXIMUM_DURATION_END
    )


def test_active_session_with_expired_lease_fails_without_recovery() -> None:
    record = _connecting(worker_lease_expires_at=NOW + timedelta(seconds=15))
    active = record.model_copy(update={"status": SessionStatus.ACTIVE, "connect_deadline_at": None})

    assert reconcile_action(active, NOW + timedelta(seconds=10)) is ReconcileAction.NONE
    assert reconcile_action(active, NOW + timedelta(seconds=16)) is ReconcileAction.FAIL_WORKER_LOST


def test_terminal_sessions_are_left_alone() -> None:
    final = _ending(DisconnectReason.USER_ENDED).finalize_end(now=NOW)

    assert reconcile_action(final, NOW + timedelta(days=1)) is ReconcileAction.NONE
