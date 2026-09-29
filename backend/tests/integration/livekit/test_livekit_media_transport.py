"""Opt-in two-way media through the real worker transport over LiveKit Cloud (``-m livekit``).

No agent job is dispatched here: the worker side joins with a token minted by
the backend for the session's opaque agent identity, and runs the real
``RtcRoomGateway`` + ``LiveKitSessionTransport`` + media check (test tone). A
simulated browser joins with the control API's scoped join token. Verified:

- browser microphone frames reach the worker at 16 kHz (resampled once);
- the ``agent-audio`` test tone is audible in the browser;
- ``va.playback.v1`` carries the ack identity, and a browser ack arrives;
- a browser disconnect and rejoin with a refreshed token (same identity)
  inside the reconnect window yields ``browser_left`` -> ``reconnected``;
- the room is deleted.

Metered but tiny (one room, ~20 s). Nothing sensitive is printed.

Selection: ``-m livekit`` runs this module and the control-plane check but
not the dispatched-job test in ``tests/integration/persistence/
test_livekit_media.py``, which needs ``-m "livekit and atlas"``.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from livekit import api, rtc
from tests.support.livekit_browser import AUDIBLE_PEAK, Browser

from voice_agent.agent_worker.media_check import MediaCheck, MediaTiming
from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.transport import TransportEventKind
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.ports.transport_control import TransportAllocation
from voice_agent.provider_registry.media_check_config import MEDIA_CHECK_AGENT_CONFIG_ID
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import BootstrapSettings
from voice_agent.transport_adapters.livekit.control import LiveKitTransportControl
from voice_agent.transport_adapters.livekit.rtc_binding import RtcRoomGateway, SoxResampler
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

pytestmark = [
    pytest.mark.livekit,
    pytest.mark.asyncio,
    # Known SDK/driver-internal leftovers only (traced): the LiveKit job/rtc
    # thread's event loop and sockets, and pymongo monitor sockets, are left for
    # the GC after close. Any other ResourceWarning still fails the test.
    pytest.mark.filterwarnings("ignore:unclosed <socket.socket:ResourceWarning"),
    pytest.mark.filterwarnings("ignore:unclosed event loop:ResourceWarning"),
    pytest.mark.filterwarnings(
        "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning"
    ),
    pytest.mark.filterwarnings(
        "ignore:Exception ignored in. <function BaseEventLoop.__del__"
        ":pytest.PytestUnraisableExceptionWarning"
    ),
]


def _settings() -> BootstrapSettings:
    settings = load_bootstrap_configuration().settings
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        pytest.skip("LiveKit is not configured")
    return settings


def _agent_token(settings: BootstrapSettings, allocation: TransportAllocation) -> str:
    key, secret = settings.livekit_api_key, settings.livekit_api_secret
    assert key is not None
    assert secret is not None
    assert allocation.agent_identity is not None
    grants = api.VideoGrants(
        room_join=True, room=allocation.room_name, can_publish=True, can_subscribe=True
    )
    return (
        api.AccessToken(key.get_secret_value(), secret.get_secret_value())
        .with_identity(allocation.agent_identity)
        .with_kind("agent")
        .with_ttl(timedelta(minutes=5))
        .with_grants(grants)
        .to_jwt()
    )


async def _until(check: Callable[[], Awaitable[bool] | bool], *, seconds: float) -> None:
    for _attempt in range(int(seconds / 0.2)):
        result = check()
        if (await result) if asyncio.iscoroutine(result) else result:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not reached in time")


async def test_two_way_media_and_rejoin_through_the_worker_transport() -> None:
    settings = _settings()
    assert settings.livekit_url is not None
    assert settings.livekit_api_key is not None
    assert settings.livekit_api_secret is not None
    control = LiveKitTransportControl(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    session_id = str(uuid.uuid4())
    locator = DispatchLocator(
        session_id=session_id,
        correlation_id="wp6-livekit-media-transport",
        agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID,
        environment=settings.app_env.value,
    )
    allocation = await control.prepare_session(
        locator, agent_name=f"wp6-none-{uuid.uuid4().hex[:8]}"
    )
    agent_room = rtc.Room()
    token = _agent_token(settings, allocation)
    url = settings.livekit_url

    async def connect() -> None:
        await agent_room.connect(url, token, options=rtc.RoomOptions(auto_subscribe=True))

    assert allocation.agent_identity is not None
    transport = LiveKitSessionTransport(
        session_id=session_id,
        browser_identity=allocation.participant_identity,
        agent_identity=allocation.agent_identity,
        worker_generation=1,
        gateway=RtcRoomGateway(agent_room, connect=connect),
        clock=SystemClock(),
        resampler_factory=SoxResampler,
        reconnect_window_ms=10_000,
    )
    kinds: list[TransportEventKind] = []
    browser = Browser()
    rejoined = Browser()
    try:
        await transport.connect()
        watcher = asyncio.create_task(_collect(transport, kinds))
        check = MediaCheck(
            transport,
            session_id=session_id,
            correlation_id=locator.correlation_id,
            worker_generation=1,
            clock=SystemClock(),
            ids=UuidIdGenerator(),
            timing=MediaTiming(burst_ms=600, period_ms=900, metrics_interval_s=0.5),
        )
        media = asyncio.create_task(check.run())
        credential = await control.issue_join_token(allocation, now=SystemClock().utc_now())
        browser.listen()
        await browser.room.connect(url, credential.token)
        await browser.publish_microphone()

        await _until(lambda: check.mic_frames > 50, seconds=20)
        async with asyncio.timeout(20):
            await browser.heard_agent.wait()
        await _until(lambda: bool(browser.playback_payloads()), seconds=10)
        first = browser.playback_payloads()[0]
        ack = {k: first[k] for k in ("worker_generation", "cancellation_generation", "segment_id")}
        await browser.send_client(session_id, "client.ready", {})
        await browser.send_client(session_id, "playback.started", ack)
        await _until(lambda: check.acks.get("started", 0) >= 1, seconds=10)

        await browser.close()
        await _until(lambda: TransportEventKind.BROWSER_LEFT in kinds, seconds=20)
        refreshed = await control.issue_join_token(allocation, now=SystemClock().utc_now())
        await rejoined.room.connect(url, refreshed.token)
        await _until(lambda: TransportEventKind.RECONNECTED in kinds, seconds=10)
        media.cancel()
        watcher.cancel()
        usage = transport.usage()
    finally:
        await transport.close()
        await rejoined.close()
        await asyncio.gather(control.release_session(allocation), return_exceptions=True)
        gone = not (await control.inspect_session(allocation)).room_exists
        await control.aclose()
        gc.collect()  # SDK leftovers are collected inside the narrow filters

    assert usage.microphone_frames > 50
    assert usage.published_frames > 0
    assert browser.agent_peak > AUDIBLE_PEAK
    assert set(first) >= {"state", "worker_generation", "cancellation_generation", "segment_id"}
    assert usage.reconnect_count == 1
    assert TransportEventKind.RECONNECT_EXPIRED not in kinds
    assert gone


async def _collect(transport: LiveKitSessionTransport, kinds: list[TransportEventKind]) -> None:
    async for event in transport.lifecycle_events():
        kinds.append(event.kind)
