"""Session creation, idempotency, reads, and restricted fields (docs/04 §6, §8, §22, §24)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, Api, ApiFactory, create_body, new_id

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.provider_registry.mock_config import mock_agent_config_document
from voice_agent.security.overrides import (
    BROWSER_SESSION_CREATE_FIELDS,
    RESTRICTED_BROWSER_FIELDS,
)
from voice_agent.transport_adapters.mock.control import MOCK_TOKEN_PREFIX

pytestmark = pytest.mark.asyncio


async def test_first_create_returns_201_with_scoped_join_data(api: Api) -> None:
    response = await api.create_session()

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"idempotent_replay", "session", "transport", "configuration", "request_id"}
    assert body["idempotent_replay"] is False
    assert body["session"]["status"] == "connecting"
    assert body["session"]["maximum_session_ms"] == 1_800_000
    transport = body["transport"]
    assert transport["join_token"].startswith(MOCK_TOKEN_PREFIX)
    assert set(transport) == {
        "provider",
        "url",
        "room_name",
        "participant_identity",
        "join_token",
        "token_expires_at",
    }
    assert body["configuration"]["stt"] == "Mock STT"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_join_token_is_never_persisted(api: Api) -> None:
    response = await api.create_session()
    token = response.json()["transport"]["join_token"]
    record = await api.sessions.get(response.json()["session"]["session_id"])

    assert record is not None
    assert token not in record.model_dump_json()


async def test_identical_retry_reuses_session_and_room_with_fresh_token(api: Api) -> None:
    body = create_body()
    first = await api.client.post(f"{API}/sessions", json=body)
    replay = await api.client.post(f"{API}/sessions", json=body)

    assert (first.status_code, replay.status_code) == (201, 200)
    one, two = first.json(), replay.json()
    assert two["idempotent_replay"] is True
    assert one["session"]["session_id"] == two["session"]["session_id"]
    assert one["transport"]["room_name"] == two["transport"]["room_name"]
    assert one["transport"]["participant_identity"] == two["transport"]["participant_identity"]
    assert one["transport"]["join_token"] != two["transport"]["join_token"]
    assert len(api.transport.prepared) == 1


async def test_same_request_id_with_different_semantics_conflicts(api: Api) -> None:
    body = create_body()
    await api.client.post(f"{API}/sessions", json=body)

    # Detected before configuration lookup, so any other agent_config_id conflicts.
    conflict = await api.client.post(f"{API}/sessions", json={**body, "agent_config_id": new_id()})

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(api.transport.prepared) == 1


async def test_terminal_replay_returns_summary_without_token(api: Api) -> None:
    body = create_body()
    created = await api.client.post(f"{API}/sessions", json=body)
    session_id = created.json()["session"]["session_id"]
    record = await api.sessions.get(session_id)
    assert record is not None
    failed = record.fail(DisconnectReason.NETWORK_LOST, now=api.clock.utc_now())
    await api.sessions.replace(failed, expected_revision=record.state_revision)

    replay = await api.client.post(f"{API}/sessions", json=body)

    assert replay.status_code == 200
    data = replay.json()
    assert data["idempotent_replay"] is True
    assert data["session"]["status"] == "failed"
    assert "join_token" not in data["transport"]
    assert "token_expires_at" not in data["transport"]


async def test_unknown_or_inactive_configuration_is_rejected(api_factory: ApiFactory) -> None:
    retired = mock_agent_config_document(
        agent_config_id="00000000-0000-4000-8000-00000000c0f3",
        status="retired",
        retired_at="2026-09-28T00:00:00Z",
    )
    documents = [mock_agent_config_document(), retired]
    async with api_factory(documents=documents) as harness:
        unknown = await harness.create_session(agent_config_id=new_id())
        inactive = await harness.create_session(agent_config_id=retired["agent_config_id"])

    for response in (unknown, inactive):
        assert response.status_code == 422
        assert response.json()["error"]["field_errors"] == [
            {"field": "body.agent_config_id", "code": "agent_config_not_available"}
        ]


@pytest.mark.parametrize("field", sorted(RESTRICTED_BROWSER_FIELDS))
async def test_restricted_fields_cannot_be_submitted(api: Api, field: str) -> None:
    response = await api.create_session(**{field: "gpt-6-luna"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert {"field": f"body.{field}", "code": "extra_forbidden"} in error["field_errors"]
    assert "gpt-6-luna" not in response.text
    assert api.transport.prepared == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", "phone"),
        ("session_mode", "benchmark"),
        ("language_mode", "hindi"),
        ("client_request_id", "not-a-uuid"),
        ("client_request_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("agent_config_id", 12),
    ],
)
async def test_fixed_and_canonical_values_are_enforced(api: Api, field: str, value: Any) -> None:
    response = await api.create_session(**{field: value})

    assert response.status_code == 422
    fields = [item["field"] for item in response.json()["error"]["field_errors"]]
    assert f"body.{field}" in fields
    assert str(value) not in response.text


async def test_unknown_field_name_is_not_echoed(api: Api) -> None:
    canary = "sk-canaryLeakedKeyAsFieldName0123456789"
    response = await api.create_session(**{canary: "x"})

    assert response.status_code == 422
    assert canary not in response.text
    assert response.json()["error"]["field_errors"] == [
        {"field": "body.<unrecognized>", "code": "extra_forbidden"}
    ]


async def test_create_request_fields_match_the_browser_override_allowlist() -> None:
    from voice_agent.control_api.schemas.sessions import SessionCreateRequest

    assert set(SessionCreateRequest.model_fields) == BROWSER_SESSION_CREATE_FIELDS


async def test_malformed_json_is_a_validation_failure(api: Api) -> None:
    response = await api.client.post(
        f"{API}/sessions", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_dispatch_failure_issues_no_token_and_fails_session(api: Api) -> None:
    api.transport.fail_prepare = True

    response = await api.create_session()

    assert response.status_code == 503
    assert "join_token" not in response.text
    page = await api.client.get(f"{API}/sessions")
    (item,) = page.json()["items"]
    assert item["status"] == SessionStatus.FAILED.value
    assert item["disconnect_reason"] == "transport_error"


async def test_token_failure_releases_dispatch_and_fails_session(api: Api) -> None:
    api.transport.fail_token = True

    response = await api.create_session()

    assert response.status_code == 503
    assert len(api.transport.released) == 1
    (item,) = (await api.client.get(f"{API}/sessions")).json()["items"]
    assert item["status"] == "failed"


async def test_get_session_summary_is_browser_safe(api: Api) -> None:
    session_id = await api.new_session_id()

    response = await api.client.get(f"{API}/sessions/{session_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recording"] == {"mode": "off", "status": "not_requested"}
    assert data["cost_summary"]["calculation_status"] == "unavailable"
    text = response.text
    for forbidden in (
        "system_instruction",
        "credential_ref",
        "client_request_id",
        "fingerprint",
        "join_token",
        "mock_voice",
        "dispatch",
        "correlation_id",
    ):
        assert forbidden not in text


async def test_unknown_session_and_invalid_id(api: Api) -> None:
    missing = await api.client.get(f"{API}/sessions/{new_id()}")
    invalid = await api.client.get(f"{API}/sessions/not-a-uuid")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["field_errors"][0]["field"] == "path.session_id"


async def test_session_list_is_newest_first_with_cursor(api: Api) -> None:
    created = []
    for _ in range(3):
        created.append(await api.new_session_id())
        api.clock.advance(1000)

    first = await api.client.get(f"{API}/sessions", params={"limit": 2})
    cursor = first.json()["next_cursor"]
    second = await api.client.get(f"{API}/sessions", params={"limit": 2, "cursor": cursor})

    ids = [item["session_id"] for item in first.json()["items"]]
    assert ids == [created[2], created[1]]
    assert [item["session_id"] for item in second.json()["items"]] == [created[0]]
    assert second.json()["next_cursor"] is None


async def test_session_list_filters(api: Api) -> None:
    older = await api.new_session_id()
    api.clock.advance(5000)
    newer = await api.new_session_id()
    before = (api.clock.utc_now() - timedelta(seconds=1)).isoformat()

    by_time = await api.client.get(f"{API}/sessions", params={"created_before": before})
    by_status = await api.client.get(f"{API}/sessions", params={"status": "active"})
    by_config = await api.client.get(f"{API}/sessions", params={"agent_config_id": new_id()})

    assert [i["session_id"] for i in by_time.json()["items"]] == [older]
    assert by_status.json()["items"] == []
    assert by_config.json()["items"] == []
    assert newer != older


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"status": "paused"},
        {"cursor": "!!notbase64"},
        {"created_before": "1700000000"},
        {"created_before": "2026-09-28T00:00:00"},
        {"unknown": "x"},
    ],
    ids=["limit_low", "limit_high", "status", "cursor", "epoch", "naive", "unknown"],
)
async def test_session_list_rejects_invalid_query(api: Api, params: dict[str, Any]) -> None:
    response = await api.client.get(f"{API}/sessions", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_cursor_from_another_listing_is_rejected(api: Api) -> None:
    from voice_agent.control_api.cursors import encode_cursor

    foreign = encode_cursor("turns", {"s": 3})

    response = await api.client.get(f"{API}/sessions", params={"cursor": foreign})

    assert response.status_code == 422
    assert response.json()["error"]["field_errors"] == [
        {"field": "query.cursor", "code": "cursor_invalid"}
    ]


async def test_agent_config_routes(api: Api) -> None:
    listing = await api.client.get(f"{API}/agent-configs")
    other_env = await api.client.get(f"{API}/agent-configs", params={"environment": "rd"})
    item = listing.json()["items"][0]
    detail = await api.client.get(f"{API}/agent-configs/{item['agent_config_id']}")
    missing = await api.client.get(f"{API}/agent-configs/{new_id()}")
    bad_status = await api.client.get(f"{API}/agent-configs", params={"status": "draft"})

    assert listing.status_code == 200
    assert listing.json()["next_cursor"] is None
    assert other_env.json()["items"] == []
    assert detail.json()["data"] == item
    assert item["features"] == {"partial_transcripts": True, "interruptions": True}
    assert missing.status_code == 404
    assert bad_status.status_code == 422
    for forbidden in ("system_instruction", "credential_ref", "safe_options", "timeout_policy"):
        assert forbidden not in listing.text + detail.text
