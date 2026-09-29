"""LiveKit control-plane adapter against a fake server API (docs/06 §3-§6, §17, §19).

Token claims are verified with the real ``livekit-api`` verifier using a
throwaway test key pair; no network call is made.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from livekit import api
from pydantic import SecretStr
from tests.support.fake_livekit_api import TEST_KEY, TEST_SECRET, FakeApi

from voice_agent.contracts.dispatch import DispatchLocator, decode_dispatch_metadata
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.realtime_wire import CONTROL_TOPIC, EndRequestedSignal
from voice_agent.ports.transport_control import (
    JOIN_TOKEN_LIFETIME_S,
    TransportAllocation,
    TransportControl,
    TransportControlError,
)
from voice_agent.transport_adapters.livekit.control import (
    LIVEKIT_PROVIDER,
    MAX_ROOM_PARTICIPANTS,
    LiveKitTransportControl,
)

URL = "wss://example-project.livekit.cloud"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.asyncio

LOCATOR = DispatchLocator(
    session_id="00000000-0000-4000-8000-000000000001",
    correlation_id="corr-1",
    agent_config_id="00000000-0000-4000-8000-000000000002",
    environment="development",
)


def _control(fake: FakeApi) -> LiveKitTransportControl:
    return LiveKitTransportControl(
        url=URL,
        api_key=SecretStr(TEST_KEY),
        api_secret=SecretStr(TEST_SECRET),
        api_factory=lambda: fake,
    )


async def test_satisfies_the_port() -> None:
    control = _control(FakeApi())

    assert isinstance(control, TransportControl)
    assert control.provider == LIVEKIT_PROVIDER
    assert control.is_available
    assert control.public_url == URL


async def test_secrets_never_appear_in_repr() -> None:
    control = _control(FakeApi())

    assert TEST_SECRET not in repr(control)
    assert TEST_KEY not in repr(control)


async def test_prepare_creates_bounded_room_then_explicit_dispatch() -> None:
    fake = FakeApi()

    allocation = await _control(fake).prepare_session(LOCATOR, agent_name="phase0-voice-agent")

    assert allocation.room_name.startswith("va-rd-")
    assert allocation.participant_identity.startswith("va-user-")
    assert allocation.agent_identity is not None
    assert allocation.agent_identity.startswith("va-agent-")
    assert allocation.dispatch_id == "AD_1"
    room = fake.rooms[allocation.room_name]
    assert room.max_participants == MAX_ROOM_PARTICIPANTS
    assert room.empty_timeout > 0
    assert room.departure_timeout > 0
    [dispatch] = fake.dispatches
    assert dispatch.agent_name == "phase0-voice-agent"
    assert dispatch.room == allocation.room_name
    assert decode_dispatch_metadata(dispatch.metadata) == LOCATOR
    assert LOCATOR.session_id not in allocation.room_name


async def test_identities_are_opaque_and_unique() -> None:
    control = _control(FakeApi())

    first = await control.prepare_session(LOCATOR, agent_name="a")
    second = await control.prepare_session(LOCATOR, agent_name="a")

    assert len({first.room_name, second.room_name}) == 2
    assert first.participant_identity != first.agent_identity


def _verify(token: str) -> api.Claims:
    return api.TokenVerifier(TEST_KEY, TEST_SECRET).verify(token)


async def test_join_token_is_room_and_identity_scoped_least_privilege() -> None:
    control = _control(FakeApi())
    allocation = await control.prepare_session(LOCATOR, agent_name="a")

    credential = await control.issue_join_token(allocation, now=NOW)
    claims = _verify(credential.token)

    assert credential.expires_at == NOW + timedelta(seconds=JOIN_TOKEN_LIFETIME_S)
    assert credential.token not in repr(credential)
    assert claims.identity == allocation.participant_identity
    video = claims.video
    assert video is not None
    assert video.room == allocation.room_name
    assert video.room_join is True
    assert video.can_subscribe is True
    assert video.can_publish_data is True
    assert video.can_publish_sources == ["microphone"]
    assert not video.room_admin
    assert not video.room_create
    assert not video.room_list
    assert not video.room_record
    assert not video.can_update_own_metadata
    assert not video.agent
    assert not claims.metadata


async def test_join_token_expires_within_ten_minutes() -> None:
    control = _control(FakeApi())
    allocation = await control.prepare_session(LOCATOR, agent_name="a")

    credential = await control.issue_join_token(allocation, now=NOW)
    body = credential.token.split(".")[1]
    padded = body + "=" * (-len(body) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))

    assert 0 < claims["exp"] - claims["nbf"] <= JOIN_TOKEN_LIFETIME_S


async def test_join_token_requires_a_livekit_allocation() -> None:
    control = _control(FakeApi())
    foreign = TransportAllocation(
        provider="mock_transport", room_name="r", participant_identity="p", dispatch_id=None
    )

    with pytest.raises(TransportControlError):
        await control.issue_join_token(foreign, now=NOW)


async def test_dispatch_failure_cleans_up_the_room_and_normalizes() -> None:
    provider_text = "raw provider failure with details"
    fake = FakeApi({"create_dispatch": api.ServerError("internal", provider_text, status=500)})

    with pytest.raises(TransportControlError) as caught:
        await _control(fake).prepare_session(LOCATOR, agent_name="a")

    assert caught.value.code == "dispatch_failed"
    assert provider_text not in str(caught.value)
    assert caught.value.__cause__ is None
    assert fake.rooms == {}


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (api.ServerError("unauthenticated", "x", status=401), "authentication_failed", False),
        (api.ServerError("permission_denied", "x", status=403), "authentication_failed", False),
        (api.ServerError("resource_exhausted", "x", status=429), "rate_limited", True),
        (api.ServerError("unavailable", "x", status=503), "transport_unavailable", True),
        (TimeoutError(), "transport_unavailable", True),
        (OSError("network address 10.0.0.1"), "transport_unavailable", True),
    ],
)
async def test_room_failures_are_normalized(failure: Exception, code: str, retryable: bool) -> None:
    fake = FakeApi({"create_room": failure})

    with pytest.raises(TransportControlError) as caught:
        await _control(fake).prepare_session(LOCATOR, agent_name="a")

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "10.0.0.1" not in str(caught.value)


async def test_release_deletes_dispatch_and_room_idempotently() -> None:
    fake = FakeApi()
    control = _control(fake)
    allocation = await control.prepare_session(LOCATOR, agent_name="a")

    await control.release_session(allocation)
    await control.release_session(allocation)

    assert fake.rooms == {}
    assert fake.deleted_dispatches == ["AD_1", "AD_1"]


async def test_release_reports_cleanup_failure_after_trying_every_step() -> None:
    fake = FakeApi({"delete_dispatch": api.ServerError("internal", "x", status=500)})
    control = _control(fake)
    allocation = await control.prepare_session(LOCATOR, agent_name="a")

    with pytest.raises(TransportControlError) as caught:
        await control.release_session(allocation)

    assert caught.value.code == "cleanup_failed"
    assert fake.rooms == {}


async def test_inspect_reports_safe_membership() -> None:
    fake = FakeApi()
    control = _control(fake)
    allocation = await control.prepare_session(LOCATOR, agent_name="a")
    fake.participants[allocation.room_name] = [allocation.participant_identity]

    status = await control.inspect_session(allocation)

    assert status.room_exists
    assert status.browser_present
    assert not status.agent_present
    assert status.participant_count == 1
    assert status.dispatch_present is True


async def test_inspect_missing_room() -> None:
    control = _control(FakeApi())
    allocation = TransportAllocation(
        provider=LIVEKIT_PROVIDER,
        room_name="va-rd-gone",
        participant_identity="va-user-x",
        dispatch_id=None,
        agent_identity="va-agent-x",
    )

    status = await control.inspect_session(allocation)

    assert not status.room_exists
    assert status.participant_count == 0
    assert status.dispatch_present is None


async def test_end_signal_is_reliable_and_targeted_to_the_agent() -> None:
    fake = FakeApi()
    control = _control(fake)
    allocation = await control.prepare_session(LOCATOR, agent_name="a")
    signal = EndRequestedSignal.build(
        event_id="00000000-0000-4000-8000-000000000009",
        session_id=LOCATOR.session_id,
        correlation_id=LOCATOR.correlation_id,
        occurred_at=NOW,
        termination_request_revision=1,
        reason=DisconnectReason.USER_ENDED,
    )

    await control.notify_end_requested(allocation, signal)

    [sent] = fake.sent
    assert sent.topic == CONTROL_TOPIC
    assert list(sent.destination_identities) == [allocation.agent_identity]
    assert sent.kind == api.DataPacket.Kind.RELIABLE
    assert json.loads(sent.data)["event_type"] == "session.end_requested"


async def test_end_signal_without_agent_identity_is_rejected() -> None:
    control = _control(FakeApi())
    allocation = TransportAllocation(
        provider=LIVEKIT_PROVIDER, room_name="r", participant_identity="p", dispatch_id=None
    )
    signal = EndRequestedSignal.build(
        event_id="00000000-0000-4000-8000-000000000009",
        session_id=LOCATOR.session_id,
        correlation_id="c",
        occurred_at=NOW,
        termination_request_revision=1,
        reason=DisconnectReason.USER_ENDED,
    )

    with pytest.raises(TransportControlError):
        await control.notify_end_requested(allocation, signal)


async def test_client_is_created_lazily_and_closed_once() -> None:
    fake = FakeApi()
    built: list[Any] = []

    def factory() -> FakeApi:
        built.append(fake)
        return fake

    control = LiveKitTransportControl(
        url=URL, api_key=SecretStr(TEST_KEY), api_secret=SecretStr(TEST_SECRET), api_factory=factory
    )
    await control.aclose()
    assert built == []

    await control.prepare_session(LOCATOR, agent_name="a")
    await control.prepare_session(LOCATOR, agent_name="a")
    await control.aclose()
    await control.aclose()

    assert len(built) == 1
    assert fake.closed == 1


@pytest.mark.asyncio
async def test_inspect_tolerates_a_room_deleted_between_calls() -> None:
    """Regression: cleanup racing an inspection surfaced as a control failure."""
    fake = FakeApi()
    control = _control(fake)
    allocation = await control.prepare_session(LOCATOR, agent_name="a")
    fake.failures["list_participants"] = api.ServerError("not_found", "gone", status=404)

    status = await control.inspect_session(allocation)

    assert not status.room_exists
    fake.failures = {"get_dispatch": api.ServerError("not_found", "gone", status=404)}
    assert (await control.inspect_session(allocation)).dispatch_present is False
