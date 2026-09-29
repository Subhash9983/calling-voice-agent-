"""Two-way media over LiveKit Cloud without AI providers (``-m "livekit and atlas"``).

Selection: this dispatched-job test carries both markers and needs the real
R&D database, so ``-m livekit`` alone skips it (the ``fake`` backend
parameter skips itself); run it with ``-m "livekit and atlas"``. It also needs
LiveKit Cloud agent session recording/observability to be off, otherwise the
worker rejects the job by policy and the test skips with that reason.

Metered but tiny: one room, well under a minute, always deleted. The real
worker runs in-process under a unique agent name (so a manually running
worker never receives the dispatch), the control API runs in MongoDB mode on
the R&D database with the real LiveKit control adapter, and a simulated
browser joins with the backend-issued join token and publishes a synthetic
48 kHz microphone tone. The test verifies:

- the worker received microphone frames (``va.metrics.v1`` ``mic_frames``);
- the browser received audible ``agent-audio`` test tone;
- ``va.playback.v1`` carries the ack identity and a browser ack is counted;
- ``POST /end`` (fast signal + durable request) reaches ``ended``;
- the room is gone afterwards.

Credentials load only through the WP3 loader; nothing sensitive is printed.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from tests.integration.control_api.conftest import API, ApiFactory, new_id
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV
from tests.support.livekit_browser import AUDIBLE_PEAK, Browser

from voice_agent.agent_worker.entrypoint import WorkerConfig, new_worker_instance_id
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.server import build_server, install_config
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.transport_control import TransportAllocation
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.media_check_config import MEDIA_CHECK_AGENT_CONFIG_ID
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.readiness import PersistenceMode
from voice_agent.security.settings import BootstrapSettings
from voice_agent.transport_adapters.livekit.control import LiveKitTransportControl

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


def _control(settings: BootstrapSettings) -> LiveKitTransportControl:
    assert settings.livekit_url
    assert settings.livekit_api_key
    assert settings.livekit_api_secret
    return LiveKitTransportControl(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


class _Rejections(logging.Handler):
    """Collects the worker's safe job-rejection reasons (no payloads)."""

    def __init__(self) -> None:
        super().__init__()
        self.reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "safe_fields", None)
        if record.getMessage() == "worker.job_rejected" and isinstance(fields, dict):
            self.reasons.append(str(fields.get("reason")))


@pytest.fixture
def rejections() -> Iterator[_Rejections]:
    handler = _Rejections()
    logger = logging.getLogger("voice_agent.agent_worker")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest_asyncio.fixture
async def worker_name(backend: Backend) -> AsyncIterator[str]:
    """Run the real worker in-process under a unique agent name."""
    if not backend.is_atlas:
        pytest.skip("the in-process worker writes the real R&D database")
    settings = _settings()
    name = f"wp6-it-{uuid.uuid4().hex[:8]}"
    config = WorkerConfig(
        settings=settings.model_copy(update={"app_agent_name": name}),
        media_mode=MediaMode.TONE,
        worker_instance_id=new_worker_instance_id(),
    )
    install_config(config)
    server = build_server(config, health_port=0)
    registered = asyncio.Event()
    server.on("worker_registered", lambda *_args: registered.set())
    running = asyncio.create_task(server.run(devmode=False))
    try:
        async with asyncio.timeout(30):
            await registered.wait()
        yield name
    finally:
        await server.aclose()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        # Collect the SDK job thread's abandoned loop/self-pipe now, inside this
        # test's narrow warning filters, rather than at interpreter teardown.
        gc.collect()


async def _until(check: Callable[[], Awaitable[bool]], *, seconds: float) -> None:
    for _attempt in range(int(seconds / 0.25)):
        if await check():
            return
        await asyncio.sleep(0.25)
    raise AssertionError("condition not reached in time")


async def _status(backend: Backend, session_id: str) -> str:
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": session_id}, projection={"status": 1, "_id": 0}
    )
    return "" if raw is None else str(raw["status"])


async def test_two_way_media_without_ai_providers(
    backend: Backend, api_factory: ApiFactory, worker_name: str, rejections: _Rejections
) -> None:
    settings = _settings()
    # Both built-in configurations are left in place: readiness needs the
    # default (mock) one and the manual browser test uses the media check.
    await seed_agent_configs(backend.persistence, builtin_agent_config_documents())
    control = _control(settings)
    env = {**MONGO_ENV, "APP_AGENT_NAME": worker_name}
    allocation: TransportAllocation | None = None
    browser = Browser()
    try:
        async with api_factory(
            environ=env,
            persistence=PersistenceMode.MONGODB,
            mongo=backend.persistence,
            extra_transports={"livekit": control},
        ) as api:
            api.clock.advance(_ms_until_now(api.clock.utc_now()))
            created = await api.create_session(agent_config_id=MEDIA_CHECK_AGENT_CONFIG_ID)
            # Safe fields only: the error envelope never carries secrets.
            error = created.json().get("error") if created.status_code != 201 else None
            assert created.status_code == 201, (created.status_code, error)
            body = created.json()
            session_id = body["session"]["session_id"]
            backend.tracker.sessions.add(session_id)
            transport = body["transport"]
            allocation = TransportAllocation(
                provider="livekit",
                room_name=transport["room_name"],
                participant_identity=transport["participant_identity"],
                dispatch_id=None,
            )
            browser.listen()
            assert settings.livekit_url is not None
            await browser.room.connect(settings.livekit_url, transport["join_token"])
            await browser.publish_microphone()

            async def active() -> bool:
                if "recording_enabled" in rejections.reasons:
                    pytest.skip(
                        "LiveKit Cloud marks jobs enable_recording (agent session recording/"
                        "observability); the worker rejects them by policy. Disable it for the "
                        "project to run this test."
                    )
                return await _status(backend, session_id) == "active"

            await _until(active, seconds=30)
            async with asyncio.timeout(30):
                await browser.heard_agent.wait()

            async def mic_seen() -> bool:
                return browser.worker_mic_frames() > 10

            await _until(mic_seen, seconds=15)
            [first, *_rest] = browser.playback_payloads()
            await browser.send_client(session_id, "client.ready", {})
            ack = {
                k: first[k] for k in ("worker_generation", "cancellation_generation", "segment_id")
            }
            await browser.send_client(session_id, "playback.started", ack)
            await asyncio.sleep(1.5)
            ended = await api.client.post(
                f"{API}/sessions/{session_id}/end",
                json={"client_request_id": new_id(), "reason": "user_ended"},
            )
            assert ended.status_code == 202

            async def finished() -> bool:
                return await _status(backend, session_id) == "ended"

            await _until(finished, seconds=30)
        raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
            {"session_id": session_id}
        )
        await _until(lambda: _room_gone(control, allocation), seconds=15)
    finally:
        await browser.close()
        if allocation is not None:
            await asyncio.gather(control.release_session(allocation), return_exceptions=True)
        await control.aclose()

    assert raw is not None
    assert raw["disconnect_reason"] == "user_ended"
    assert raw["transport"]["agent_participant_id"].startswith("va-agent-")
    assert browser.agent_peak > AUDIBLE_PEAK
    assert set(first) >= {"state", "worker_generation", "cancellation_generation", "segment_id"}
    assert any(
        body["payload"].get("playback_acks", {}).get("started", 0) >= 1
        for topic, body in browser.messages
        if topic == "va.metrics.v1"
    )


async def _room_gone(
    control: LiveKitTransportControl, allocation: TransportAllocation | None
) -> bool:
    assert allocation is not None
    return not (await control.inspect_session(allocation)).room_exists


def _ms_until_now(frozen: datetime) -> int:
    return max(0, int((datetime.now(UTC) - frozen).total_seconds() * 1000) + 1000)
