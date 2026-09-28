"""Join-token refresh and idempotent session end (docs/04 §7, §9)."""

from __future__ import annotations

import pytest
from tests.integration.control_api.conftest import API, Api, new_id

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.contracts.events import EventType

pytestmark = pytest.mark.asyncio


async def _events(api: Api, session_id: str) -> list[str]:
    response = await api.client.get(f"{API}/sessions/{session_id}/events")
    return [item["event_type"] for item in response.json()["items"]]


async def test_join_token_first_and_identical_replay(api: Api) -> None:
    session_id = await api.new_session_id()
    body = {"client_request_id": new_id()}

    first = await api.client.post(f"{API}/sessions/{session_id}/join-token", json=body)
    replay = await api.client.post(f"{API}/sessions/{session_id}/join-token", json=body)

    assert (first.status_code, replay.status_code) == (200, 200)
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    one, two = first.json()["data"]["transport"], replay.json()["data"]["transport"]
    assert one["room_name"] == two["room_name"]
    assert one["participant_identity"] == two["participant_identity"]
    assert one["join_token"] != two["join_token"]
    record = await api.sessions.get(session_id)
    assert record is not None
    (entry,) = record.join_token_requests
    assert entry.issue_count == 2
    assert one["join_token"] not in record.model_dump_json()


async def test_join_token_refused_for_terminal_session(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await api.sessions.get(session_id)
    assert record is not None
    failed = record.fail(DisconnectReason.NETWORK_LOST, now=api.clock.utc_now())
    await api.sessions.replace(failed, expected_revision=record.state_revision)

    response = await api.client.post(
        f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert "join_token" not in response.text


async def test_join_token_refused_after_maximum_duration(api: Api) -> None:
    session_id = await api.new_session_id()
    api.clock.advance(1_800_000)

    response = await api.client.post(
        f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
    )

    assert response.status_code == 409


async def test_join_token_validation_and_missing_session(api: Api) -> None:
    missing = await api.client.post(
        f"{API}/sessions/{new_id()}/join-token", json={"client_request_id": new_id()}
    )
    extra = await api.client.post(
        f"{API}/sessions/{new_id()}/join-token",
        json={"client_request_id": new_id(), "room_name": "attacker-room"},
    )

    assert missing.status_code == 404
    assert extra.status_code == 422
    assert "attacker-room" not in extra.text


async def test_join_token_transport_failure_is_safe(api: Api) -> None:
    session_id = await api.new_session_id()
    api.transport.fail_token = True

    response = await api.client.post(
        f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
    )

    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True


async def test_end_first_request_is_accepted_and_emits_event_once(api: Api) -> None:
    session_id = await api.new_session_id()
    body = {"client_request_id": new_id(), "reason": "user_ended"}

    first = await api.client.post(f"{API}/sessions/{session_id}/end", json=body)
    replay = await api.client.post(f"{API}/sessions/{session_id}/end", json=body)
    later = await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": new_id(), "reason": "browser_closed"},
    )

    assert first.status_code == 202
    assert first.json() == {
        "data": {
            "session_id": session_id,
            "status": "ending",
            "revision": 2,
            "termination_request_revision": 1,
            "disconnect_reason": None,
        },
        "idempotent_replay": False,
        "request_id": first.headers["x-request-id"],
    }
    for response in (replay, later):
        assert response.status_code == 200
        assert response.json()["idempotent_replay"] is True
        assert response.json()["data"] == first.json()["data"]
    events = await _events(api, session_id)
    assert events.count(EventType.SESSION_END_REQUESTED.value) == 1
    record = await api.sessions.get(session_id)
    assert record is not None
    assert record.termination_request is not None
    assert record.termination_request.requested_by == "anonymous_user"


async def test_end_reused_id_with_other_reason_conflicts(api: Api) -> None:
    session_id = await api.new_session_id()
    request_id = new_id()
    await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": request_id, "reason": "user_ended"},
    )

    conflict = await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": request_id, "reason": "idle_timeout"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_end_on_terminal_session_returns_current_state(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await api.sessions.get(session_id)
    assert record is not None
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=api.clock.utc_now())
    await api.sessions.replace(failed, expected_revision=record.state_revision)

    response = await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": new_id(), "reason": "user_ended"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == SessionStatus.FAILED.value
    assert data["disconnect_reason"] == "transport_error"
    assert data["termination_request_revision"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"client_request_id": "x", "reason": "user_ended"},
        {"reason": "user_ended"},
        {"client_request_id": "00000000-0000-4000-8000-000000000001", "reason": "because"},
        {
            "client_request_id": "00000000-0000-4000-8000-000000000001",
            "reason": "user_ended",
            "requested_by": "system_reconciler",
        },
    ],
    ids=["bad_id", "missing_id", "bad_reason", "restricted_requester"],
)
async def test_end_validation(api: Api, body: dict[str, str]) -> None:
    session_id = await api.new_session_id()

    response = await api.client.post(f"{API}/sessions/{session_id}/end", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_end_while_join_token_still_available(api: Api) -> None:
    session_id = await api.new_session_id()
    await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": new_id(), "reason": "user_ended"},
    )

    response = await api.client.post(
        f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
    )

    assert response.status_code == 200
