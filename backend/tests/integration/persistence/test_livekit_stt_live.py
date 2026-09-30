"""Live STT check over LiveKit Cloud + Deepgram + Atlas (``-m "livekit and atlas and deepgram"``).

Metered but tiny (docs/15 WP7 budget): one room and one Deepgram stream for
a few seconds, always cleaned up. The real worker runs in-process in
``--media-mode stt`` with the real prewarmed Silero model, under a unique
agent name. A simulated browser publishes a synthetic 300 Hz tone (no
speech, nothing recorded), so local VAD opens no turn while the Deepgram
stream still runs; the test verifies the worker's STT wiring end to end:
``listening`` state, one succeeded ``stt_stream`` operation with provider-
reported usage, operation and session cost runs, and ``GET /costs``.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from tests.integration.control_api.conftest import API, ApiFactory, new_id
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV
from tests.integration.persistence.test_livekit_media import (
    _control,
    _ms_until_now,
    _room_gone,
    _settings,
    _status,
    _until,
)
from tests.support.livekit_browser import Browser

from voice_agent.agent_worker.entrypoint import WorkerConfig, new_worker_instance_id
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.server import build_server, install_config
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.transport_control import TransportAllocation
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.stt_check_config import STT_CHECK_AGENT_CONFIG_ID
from voice_agent.security.readiness import PersistenceMode
from voice_agent.speech_activity.silero import SileroModelHandle

pytestmark = [
    pytest.mark.livekit,
    pytest.mark.deepgram,
    pytest.mark.asyncio,
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
STREAM_SECONDS = 4.0


@pytest_asyncio.fixture
async def stt_worker(backend: Backend) -> AsyncIterator[str]:
    if not backend.is_atlas:
        pytest.skip("the in-process worker writes the real R&D database")
    settings = _settings()
    name = f"wp7-it-{uuid.uuid4().hex[:8]}"
    config = WorkerConfig(
        settings=settings.model_copy(update={"app_agent_name": name}),
        media_mode=MediaMode.STT,
        worker_instance_id=new_worker_instance_id(),
        silero=SileroModelHandle.load(),
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
        gc.collect()


async def test_stt_check_streams_to_deepgram_and_records_usage_and_cost(
    backend: Backend, api_factory: ApiFactory, stt_worker: str
) -> None:
    settings = _settings()
    await seed_agent_configs(backend.persistence, builtin_agent_config_documents())
    control = _control(settings)
    env = {**MONGO_ENV, "APP_AGENT_NAME": stt_worker}
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
            created = await api.create_session(agent_config_id=STT_CHECK_AGENT_CONFIG_ID)
            assert created.status_code == 201, created.status_code
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
                return await _status(backend, session_id) == "active"

            await _until(active, seconds=30)
            await asyncio.sleep(STREAM_SECONDS)
            ended = await api.client.post(
                f"{API}/sessions/{session_id}/end",
                json={"client_request_id": new_id(), "reason": "user_ended"},
            )
            assert ended.status_code == 202

            async def finished() -> bool:
                return await _status(backend, session_id) == "ended"

            await _until(finished, seconds=30)

            async def costed() -> bool:
                response = await api.client.get(f"{API}/sessions/{session_id}/costs")
                return response.status_code == 200

            await _until(costed, seconds=15)
            costs = (await api.client.get(f"{API}/sessions/{session_id}/costs")).json()["data"]
            operations = await api.client.get(
                f"{API}/sessions/{session_id}/operations", params={"component": "stt"}
            )
        await _until(lambda: _room_gone(control, allocation), seconds=15)
    finally:
        await browser.close()
        if allocation is not None:
            await asyncio.gather(control.release_session(allocation), return_exceptions=True)
        await control.aclose()

    states = [b["payload"]["state"] for topic, b in browser.messages if topic == "va.state.v1"]
    assert "listening" in states
    [stream] = operations.json()["items"]
    assert stream["status"] == "succeeded"
    units = {u["unit"]: u for u in stream["usage"]["items"]}
    billed = Decimal(units["transcribed_audio_seconds"]["quantity"])
    assert units["transcribed_audio_seconds"]["source"] == "provider_reported"
    assert Decimal(1) < billed < Decimal(60)
    assert stream["time_to_first_result_ms"] is not None
    assert costs["calculation_status"] == "final"
    assert Decimal(costs["total_usd"]) == Decimal(stream["estimated_cost"])
    turns = await backend.database[Collection.CONVERSATION_TURNS.value].count_documents(
        {"session_id": session_id}
    )
    assert turns == 0  # a pure tone is not speech: local VAD opened no turn
