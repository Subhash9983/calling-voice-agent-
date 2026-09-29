"""Worker request handling and process startup, offline (docs/05 §2-§3; docs/06 §7)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.persistence_builders import make_session, now_ms

from voice_agent.agent_worker import server
from voice_agent.agent_worker.admission import admit_job
from voice_agent.agent_worker.entrypoint import (
    IdentityMismatchError,
    WorkerConfig,
    handle_request,
    new_worker_instance_id,
    run_job,
    transport_factory,
    worker_stores,
)
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import SessionRecord, TransportBinding
from voice_agent.persistence.mongodb.bootstrap import apply_schema
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.provider_registry.media_check_config import (
    MEDIA_CHECK_AGENT_CONFIG_ID,
    media_check_agent_config_document,
)
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.transport_adapters.livekit.gateway import RoomHandlers

READY_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": MEDIA_CHECK_AGENT_CONFIG_ID,
    "LIVEKIT_URL": "wss://wp6-unit.livekit.cloud",
    "LIVEKIT_API_KEY": "APIwp6unitkey01",
    "LIVEKIT_API_SECRET": "wp6UnitSecretValue0123456789abcdefghijkl",
    "MONGODB_URI": "mongodb+srv://wp6user:wp6pass@cluster-wp6.abc123.mongodb.net/",
}
ROOM = "va-rd-unit-room"


@dataclass
class FakeRequest:
    job: Any
    agent_name: str = "phase0-voice-agent"
    accepted: list[dict[str, str]] = field(default_factory=list)
    rejected: int = 0

    async def accept(self, *, name: str = "", identity: str = "") -> None:
        self.accepted.append({"name": name, "identity": identity})

    async def reject(self, *, terminate: bool = True) -> None:
        self.rejected += 1


def _config() -> WorkerConfig:
    settings = load_bootstrap_configuration(READY_ENV).settings
    return WorkerConfig(settings=settings, media_mode=MediaMode.TONE, worker_instance_id="w-1")


async def _seeded() -> tuple[FakeDatabase, SessionRecord]:
    database = FakeDatabase()
    persistence = MongoPersistence.from_handles(FakeClient(database), database)
    await apply_schema(database)
    config = AgentConfig.model_validate(media_check_agent_config_document())
    await MongoAgentConfigRepository(persistence).insert(config)
    record = make_session(config, now=now_ms())
    sessions = MongoSessionRecordRepository(persistence)
    await sessions.insert(record)
    binding = TransportBinding(
        provider="livekit",
        external_room_id=ROOM,
        external_session_id="AD_unit",
        browser_participant_id="va-user-unit",
        agent_participant_id="va-agent-unit",
    )
    bound = record.bind_transport(binding, now=now_ms())
    await sessions.replace(bound, expected_revision=0)
    return database, bound


def _factory(database: FakeDatabase) -> Any:
    return lambda _settings: MongoPersistence.from_handles(FakeClient(database), database)


def _job(record: SessionRecord, **overrides: Any) -> SimpleNamespace:
    metadata = encode_dispatch_metadata(
        DispatchLocator(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            agent_config_id=record.agent_config_id,
            environment="development",
        )
    )
    values: dict[str, Any] = {
        "id": "AJ_unit",
        "metadata": metadata,
        "room": SimpleNamespace(name=ROOM),
        "enable_recording": False,
        **overrides,
    }
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_request_is_accepted_with_the_opaque_agent_identity() -> None:
    database, record = await _seeded()
    request = FakeRequest(job=_job(record))

    admission = await handle_request(request, _config(), persistence_factory=_factory(database))

    assert admission is not None
    assert request.accepted == [{"name": "agent", "identity": "va-agent-unit"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_overrides", "agent_name"),
    [
        ({"enable_recording": True}, "phase0-voice-agent"),
        ({}, "someone-else"),
        ({"metadata": "{}"}, "phase0-voice-agent"),
        ({"room": SimpleNamespace(name="va-rd-other")}, "phase0-voice-agent"),
    ],
)
async def test_foreign_recording_or_invalid_jobs_are_rejected(
    job_overrides: dict[str, Any], agent_name: str
) -> None:
    database, record = await _seeded()
    request = FakeRequest(job=_job(record, **job_overrides), agent_name=agent_name)

    admission = await handle_request(
        request, _config(), persistence_factory=_factory(database), retry_s=0.3
    )

    assert admission is None
    assert request.rejected == 1
    assert request.accepted == []


@pytest.mark.asyncio
async def test_run_job_stops_before_providers_when_admission_fails() -> None:
    database, record = await _seeded()
    ctx = SimpleNamespace(job=_job(record, metadata="{}"))

    result = await run_job(ctx, _config(), persistence_factory=_factory(database))

    assert result is None


@pytest.mark.asyncio
async def test_worker_stores_include_the_livekit_cleanup_control() -> None:
    database, _record = await _seeded()
    persistence = MongoPersistence.from_handles(FakeClient(database), database)

    stores = worker_stores(persistence, _config().settings)

    assert stores.cleanup is not None
    assert "wp6UnitSecret" not in repr(stores.cleanup)
    await stores.cleanup.aclose()


def test_worker_instance_ids_are_opaque() -> None:
    first, second = new_worker_instance_id(), new_worker_instance_id()

    assert first != second
    assert first.startswith("worker-")
    uuid.UUID(hex=first.removeprefix("worker-").ljust(32, "0"))


def test_main_refuses_without_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    code = server.main([], {"APP_DEFAULT_AGENT_CONFIG_ID": MEDIA_CHECK_AGENT_CONFIG_ID})

    captured = capsys.readouterr()
    assert code == server.EXIT_REFUSED
    assert '"started": false' in captured.err
    assert "wss://" not in captured.err


def test_main_builds_a_loopback_named_agent_server(capsys: pytest.CaptureFixture[str]) -> None:
    served: list[Any] = []

    code = server.main(["--media-mode", "echo"], READY_ENV, serve=served.append)

    captured = capsys.readouterr()
    assert code == server.EXIT_OK
    [agent_server] = served
    assert agent_server._host == server.HEALTH_HOST
    assert agent_server._agent_name == "phase0-voice-agent"
    assert server._config().media_mode is MediaMode.ECHO
    for secret in (READY_ENV["LIVEKIT_API_SECRET"], READY_ENV["LIVEKIT_URL"]):
        assert secret not in captured.out + captured.err
    for name in server.SDK_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.asyncio
async def test_request_racing_session_creation_is_retried_until_connecting() -> None:
    database = FakeDatabase()
    persistence = MongoPersistence.from_handles(FakeClient(database), database)
    await apply_schema(database)
    config = AgentConfig.model_validate(media_check_agent_config_document())
    await MongoAgentConfigRepository(persistence).insert(config)
    record = make_session(config, now=now_ms())
    sessions = MongoSessionRecordRepository(persistence)
    await sessions.insert(record)  # still ``created``: the dispatch landed first
    request = FakeRequest(job=_job(record))

    async def bind_later() -> None:
        await asyncio.sleep(0.3)
        binding = TransportBinding(
            provider="livekit",
            external_room_id=ROOM,
            external_session_id="AD_unit",
            browser_participant_id="va-user-unit",
            agent_participant_id="va-agent-unit",
        )
        await sessions.replace(record.bind_transport(binding, now=now_ms()), expected_revision=0)

    binder = asyncio.create_task(bind_later())
    admission = await handle_request(
        request, _config(), persistence_factory=_factory(database), retry_s=2.0
    )
    await binder

    assert admission is not None
    assert request.accepted == [{"name": "agent", "identity": "va-agent-unit"}]


@dataclass
class _Local:
    identity: str = "va-agent-unit"


@dataclass
class _Room:
    local_participant: _Local = field(default_factory=_Local)
    remote_participants: dict[str, Any] = field(default_factory=dict)

    def on(self, event: str, callback: Any) -> Any:
        return callback


@pytest.mark.asyncio
async def test_transport_factory_verifies_the_joined_identity() -> None:
    database, record = await _seeded()
    persistence = MongoPersistence.from_handles(FakeClient(database), database)
    admission = await _admit_for(persistence, record)
    connected: list[Any] = []

    class Ctx:
        room = _Room()

        async def connect(self, *, auto_subscribe: Any) -> None:
            connected.append(auto_subscribe)

    transport = transport_factory(Ctx(), admission)(1)
    handlers = _noop_handlers()
    await transport._gateway.connect(handlers)  # type: ignore[attr-defined]
    Ctx.room.local_participant.identity = "someone-else"

    with pytest.raises(IdentityMismatchError):
        await transport._gateway.connect(handlers)  # type: ignore[attr-defined]
    assert [str(mode.value) for mode in connected] == ["audio_only", "audio_only"]


async def _admit_for(persistence: MongoPersistence, record: SessionRecord) -> Any:
    return await admit_job(
        metadata=_job(record).metadata,
        room_name=ROOM,
        sessions=MongoSessionRecordRepository(persistence),
        configs=MongoAgentConfigRepository(persistence),
        app_env="development",
        now=now_ms(),
    )


def _noop_handlers() -> Any:
    def ignore(*_args: Any) -> None:
        return None

    return RoomHandlers(
        participant_joined=ignore,
        participant_left=ignore,
        microphone_opened=ignore,
        microphone_closed=ignore,
        data_received=ignore,
        reconnecting=ignore,
        reconnected=ignore,
        disconnected=ignore,
    )
