"""Control API over the LiveKit control adapter (fake server API; docs/04 §6-§9; docs/06 §5).

- readiness and wiring choose the LiveKit adapter when URL/key/secret exist;
- session create/join-token return scoped tokens, and the token is never
  stored in the session store, timeline, or logs;
- the response shape is unchanged and exposes no agent identity;
- an accepted end sends the targeted ``va.control.v1`` wake-up only when a
  live worker lease exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from livekit import api
from pydantic import SecretStr
from tests.integration.control_api.conftest import API, Api, ApiFactory, new_id
from tests.support.fake_livekit_api import TEST_KEY, TEST_SECRET, FakeApi

from voice_agent.control_api.runtime import livekit_transport
from voice_agent.ports.control_plane import EventQuery
from voice_agent.provider_registry.media_check_config import MEDIA_CHECK_AGENT_CONFIG_ID
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.transport_adapters.livekit.control import LiveKitTransportControl
from voice_agent.transport_adapters.unavailable import UnavailableTransportControl

URL = "wss://wp6-control.livekit.cloud"
LIVEKIT_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": MEDIA_CHECK_AGENT_CONFIG_ID,
    "LIVEKIT_URL": URL,
    "LIVEKIT_API_KEY": TEST_KEY,
    "LIVEKIT_API_SECRET": TEST_SECRET,
}


def test_livekit_adapter_is_selected_only_with_complete_credentials() -> None:
    complete = load_bootstrap_configuration(LIVEKIT_ENV).settings
    partial = load_bootstrap_configuration({"LIVEKIT_URL": URL}).settings

    assert isinstance(livekit_transport(complete), LiveKitTransportControl)
    assert isinstance(livekit_transport(partial), UnavailableTransportControl)
    assert isinstance(livekit_transport(None), UnavailableTransportControl)


def _control(fake: FakeApi) -> LiveKitTransportControl:
    return LiveKitTransportControl(
        url=URL,
        api_key=SecretStr(TEST_KEY),
        api_secret=SecretStr(TEST_SECRET),
        api_factory=lambda: fake,
    )


async def _livekit_api(api_factory: ApiFactory, fake: FakeApi) -> Any:
    return api_factory(environ=LIVEKIT_ENV, extra_transports={"livekit": _control(fake)})


@pytest.mark.asyncio
async def test_readiness_is_ready_with_the_livekit_default(api_factory: ApiFactory) -> None:
    async with await _livekit_api(api_factory, FakeApi()) as harness:
        ready = await harness.client.get("/health/ready")

    assert ready.status_code == 200, ready.text


async def _create(harness: Api) -> dict[str, Any]:
    response = await harness.create_session(agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.asyncio
async def test_create_and_refresh_issue_scoped_tokens_that_are_never_stored(
    api_factory: ApiFactory, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeApi()
    caplog.set_level(logging.DEBUG)
    async with await _livekit_api(api_factory, fake) as harness:
        body = await _create(harness)
        session_id = body["session"]["session_id"]
        refreshed = await harness.client.post(
            f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
        )
        record = await harness.sessions.get(session_id)
        events = await harness.timeline.list_events(
            EventQuery(session_id=session_id, limit=50, browser_safe_only=False)
        )

    transport = body["transport"]
    fresh = refreshed.json()["data"]["transport"]
    assert transport["provider"] == "livekit"
    assert transport["url"] == URL
    assert set(transport) == {
        "provider",
        "url",
        "room_name",
        "participant_identity",
        "join_token",
        "token_expires_at",
    }
    claims = api.TokenVerifier(TEST_KEY, TEST_SECRET).verify(transport["join_token"])
    assert claims.identity == transport["participant_identity"]
    assert claims.video is not None
    assert claims.video.room == transport["room_name"]
    # A refresh re-signs for the same room/identity with a new 10-minute expiry
    # (identical bytes only when issued within the same second).
    refreshed_claims = api.TokenVerifier(TEST_KEY, TEST_SECRET).verify(fresh["join_token"])
    assert refreshed_claims.identity == transport["participant_identity"]
    assert fresh["room_name"] == transport["room_name"]
    assert fresh["token_expires_at"] >= transport["token_expires_at"]
    tokens = (transport["join_token"], fresh["join_token"])
    assert record is not None
    assert record.transport is not None
    assert record.transport.agent_participant_id is not None
    stored = record.model_dump_json() + json.dumps(
        [e.envelope.model_dump(mode="json") for e in events]
    )
    logged = caplog.text
    for token in tokens:
        assert token not in stored
        assert token not in logged
    assert TEST_SECRET not in logged
    [dispatch] = fake.dispatches
    assert dispatch.room == transport["room_name"]


@pytest.mark.asyncio
async def test_end_with_live_worker_sends_the_targeted_wake_up(api_factory: ApiFactory) -> None:
    fake = FakeApi()
    async with await _livekit_api(api_factory, fake) as harness:
        body = await _create(harness)
        session_id = body["session"]["session_id"]
        record = await harness.sessions.get(session_id)
        assert record is not None
        assert record.transport is not None
        harness.sessions._items[session_id] = record.model_copy(
            update={"worker_lease_expires_at": harness.clock.utc_now() + timedelta(seconds=10)}
        )
        ended = await harness.client.post(
            f"{API}/sessions/{session_id}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )

    assert ended.status_code == 202
    [sent] = fake.sent
    assert sent.topic == "va.control.v1"
    assert list(sent.destination_identities) == [record.transport.agent_participant_id]
    signal = json.loads(sent.data)
    assert signal["session_id"] == session_id
    assert signal["payload"] == {"termination_request_revision": 1, "reason": "user_ended"}


@pytest.mark.asyncio
async def test_end_without_worker_sends_no_packet(api_factory: ApiFactory) -> None:
    fake = FakeApi()
    async with await _livekit_api(api_factory, fake) as harness:
        body = await _create(harness)
        ended = await harness.client.post(
            f"{API}/sessions/{body['session']['session_id']}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )

    assert ended.status_code == 202
    assert fake.sent == []


@pytest.mark.asyncio
async def test_failed_wake_up_does_not_fail_the_end(api_factory: ApiFactory) -> None:
    fake = FakeApi({"send_data": api.ServerError("unavailable", "x", status=503)})
    async with await _livekit_api(api_factory, fake) as harness:
        body = await _create(harness)
        session_id = body["session"]["session_id"]
        record = await harness.sessions.get(session_id)
        assert record is not None
        harness.sessions._items[session_id] = record.model_copy(
            update={"worker_lease_expires_at": harness.clock.utc_now() + timedelta(seconds=10)}
        )
        ended = await harness.client.post(
            f"{API}/sessions/{session_id}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )

    assert ended.status_code == 202
    assert ended.json()["data"]["status"] == "ending"


@pytest.mark.asyncio
async def test_dispatch_failure_returns_no_token_and_fails_the_session(
    api_factory: ApiFactory,
) -> None:
    fake = FakeApi({"create_dispatch": api.ServerError("internal", "x", status=500)})
    async with await _livekit_api(api_factory, fake) as harness:
        response = await harness.create_session(agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID)
        [record] = harness.sessions.snapshot()

    assert response.status_code == 503
    assert "join_token" not in response.text
    assert record.status.value == "failed"
    assert fake.rooms == {}


class _SlowRooms:
    """Wraps the fake room service with realistic remote latency."""

    def __init__(self, inner: Any, delay_s: float) -> None:
        self._inner, self._delay_s = inner, delay_s

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        async def slow(*args: Any) -> Any:
            await asyncio.sleep(self._delay_s)
            return await method(*args)

        return slow


@pytest.mark.asyncio
async def test_remote_control_latency_is_not_bounded_by_the_store_timeout(
    api_factory: ApiFactory,
) -> None:
    """Regression: room + dispatch (~1.5 s on LiveKit Cloud) exceeded the 2 s store bound."""
    fake = FakeApi()
    fake.room = _SlowRooms(fake.room, 0.3)  # type: ignore[assignment]
    fake.agent_dispatch = _SlowRooms(fake.agent_dispatch, 0.3)  # type: ignore[assignment]
    async with api_factory(
        environ=LIVEKIT_ENV, extra_transports={"livekit": _control(fake)}, timeout_s=0.2
    ) as harness:
        response = await harness.create_session(agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID)

    assert response.status_code == 201, response.text
    assert "join_token" in response.json()["transport"]
