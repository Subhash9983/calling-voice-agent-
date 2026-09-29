"""Opt-in LiveKit Cloud control-plane check (``-m livekit``; metered, one short room).

Credentials load only through the WP3 loader; no URL, key, secret, or token
is printed. The room is always deleted.

Selection: ``-m livekit`` runs this check and the worker-transport media test
(``test_livekit_media_transport.py``) but excludes the dispatched-job media
test (``tests/integration/persistence/test_livekit_media.py``), which runs
only with ``-m "livekit and atlas"``.
"""

from __future__ import annotations

import uuid

import pytest
from livekit import api

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.provider_registry.media_check_config import MEDIA_CHECK_AGENT_CONFIG_ID
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import BootstrapSettings
from voice_agent.transport_adapters.livekit.control import LiveKitTransportControl


def _settings() -> BootstrapSettings:
    settings = load_bootstrap_configuration().settings
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        pytest.skip("LiveKit is not configured")
    return settings


def _control(settings: BootstrapSettings) -> LiveKitTransportControl:
    assert settings.livekit_url
    assert settings.livekit_api_key
    assert settings.livekit_api_secret
    return LiveKitTransportControl(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


@pytest.mark.livekit
@pytest.mark.asyncio
async def test_control_plane_room_dispatch_token_and_cleanup() -> None:
    settings = _settings()
    control = _control(settings)
    locator = DispatchLocator(
        session_id=str(uuid.uuid4()),
        correlation_id="wp6-livekit-control-test",
        agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID,
        environment=settings.app_env.value,
    )
    allocation = await control.prepare_session(
        locator, agent_name=f"wp6-probe-{uuid.uuid4().hex[:8]}"
    )
    try:
        status = await control.inspect_session(allocation)
        credential = await control.issue_join_token(allocation, now=SystemClock().utc_now())
        assert settings.livekit_api_key
        assert settings.livekit_api_secret
        claims = api.TokenVerifier(
            settings.livekit_api_key.get_secret_value(),
            settings.livekit_api_secret.get_secret_value(),
        ).verify(credential.token)
    finally:
        await control.release_session(allocation)
    after = await control.inspect_session(allocation)
    await control.aclose()

    assert status.room_exists
    assert status.participant_count == 0
    assert status.dispatch_present is True
    assert claims.identity == allocation.participant_identity
    assert claims.video is not None
    assert claims.video.room == allocation.room_name
    assert not after.room_exists
