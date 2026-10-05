"""WP10 conversation mode wiring: greeting -> speech -> STT -> GPT-6 Luna -> Bulbul v3 -> audio.

The real orchestrator built by ``build_conversation`` from the approved TTS
configuration, with the Deepgram/OpenAI/Sarvam seams faked and evidence in
the real MongoDB repositories over the in-process fake. Synthetic keys via
an explicit environment mapping; no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.fake_openai import FakeResponsesConnector, reply
from tests.support.fake_sarvam import FakeSarvamConnector
from tests.support.fake_sarvam import reply as speak
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport
from tests.support.fake_silero import EnergyHandle
from tests.support.persistence_builders import make_session
from tests.unit.agent_worker.test_tts_session import TTS_ENV

from voice_agent.agent_worker import server
from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.conversation_session import build_conversation
from voice_agent.agent_worker.entrypoint import WorkerConfig, session_activity, worker_stores
from voice_agent.agent_worker.llm_session import LlmSessionDeps
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.session_runner import ActivityContext, LifecycleListener
from voice_agent.agent_worker.stt_session import SttSessionDeps
from voice_agent.agent_worker.tts_session import TtsSessionDeps
from voice_agent.contracts.enums import DisconnectReason, TurnStatus
from voice_agent.contracts.transport import ClientReady
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import MongoTurnRepository
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.control_plane import EventRecord
from voice_agent.provider_registry.tts_check_config import tts_check_agent_config_document
from voice_agent.security.config_loader import load_bootstrap_configuration

pytestmark = pytest.mark.asyncio


class EventLog:
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()


def _admission() -> JobAdmission:
    config = AgentConfig.model_validate(tts_check_agent_config_document())
    record = make_session(config).model_copy(update={"session_id": SESSION_ID})
    return JobAdmission(
        locator=None,  # type: ignore[arg-type]
        record=record,
        config=config,
        agent_identity="va-agent-unit",
        browser_identity="va-user-unit",
        room_name="va-rd-unit",
    )


def _persistence() -> MongoPersistence:
    database = FakeDatabase()
    return MongoPersistence.from_handles(FakeClient(database), database)


def _worker_config(mode: MediaMode) -> WorkerConfig:
    settings = load_bootstrap_configuration(TTS_ENV).settings
    return WorkerConfig(
        settings=settings, media_mode=mode, worker_instance_id="w-10", silero=EnergyHandle()
    )


async def test_conversation_mode_selects_the_orchestrator_for_the_real_configuration() -> None:
    persistence = _persistence()
    config = _worker_config(MediaMode.CONVERSATION)
    stores = worker_stores(persistence, config.settings)

    assert session_activity(config, _admission(), persistence, stores) is not None


async def test_full_conversation_offline_greets_once_then_answers() -> None:
    admission = _admission()
    transport = FakeSessionTransport()
    persistence = _persistence()
    await seed_agent_configs(persistence, [tts_check_agent_config_document()])
    await MongoSessionRecordRepository(persistence).insert(admission.record)
    sarvam = FakeSarvamConnector.with_scripts([speak(2)] * 8)
    listeners: list[LifecycleListener] = []
    ended: list[DisconnectReason] = []
    stt = SttSessionDeps(
        settings=load_bootstrap_configuration(TTS_ENV).settings,
        silero=EnergyHandle(),  # type: ignore[arg-type]
        persistence=persistence,
        events=EventLog(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        connector_factory=lambda _key: FakeDeepgramConnector(["मेरा नाम Arun है।"]),
    )
    openai = FakeResponsesConnector([reply("Namaste Arun!")])
    deps = TtsSessionDeps(LlmSessionDeps(stt, lambda _key, _timeout: openai), lambda _k: sarvam)
    context = ActivityContext(
        transport=transport,
        session_id=SESSION_ID,
        correlation_id=admission.record.correlation_id,
        worker_generation=1,
        lease_hint=lambda: 1000,
        request_end=ended.append,
        add_lifecycle_listener=listeners.append,
    )
    orchestrator = build_conversation(context, admission, deps)

    task = asyncio.create_task(orchestrator.run())
    await asyncio.wait_for(transport.wait_for_subscribers(2), timeout=1)
    transport.client(ClientReady())
    for _ in range(100):  # the greeting finishes before the user speaks
        if any(s.body["payload"]["is_final"] for s in transport.on("va.response.v1")):
            break
        await asyncio.sleep(0.02)
    after_speech = await transport.speak(0, 20, 0.8)
    await transport.speak(after_speech, 40, 0.0)
    for _ in range(150):
        finals = [s for s in transport.on("va.response.v1") if s.body["payload"]["is_final"]]
        if len(finals) >= 2:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert listeners == [orchestrator.on_transport_event]
    assert "नमस्ते" in sarvam.texts[0]
    assert sarvam.texts[-1] == "Namaste Arun!"
    assert len(openai.params) == 1
    write = WriteContext(
        session_id=SESSION_ID,
        correlation_id=admission.record.correlation_id,
        agent_config_id=admission.record.agent_config_id,
        environment=admission.record.environment,
    )
    turns = await MongoTurnRepository(
        persistence, context=write, clock=SystemClock()
    ).list_for_session(SESSION_ID)
    assert [t.status for t in turns] == [TurnStatus.COMPLETED, TurnStatus.COMPLETED]
    assert turns[0].fallback_used
    assert turns[1].spoken_text == "Namaste Arun!"


async def test_main_accepts_the_conversation_mode(capsys: pytest.CaptureFixture[str]) -> None:
    loads: list[Any] = []

    def prewarm() -> Any:
        loads.append(1)
        return EnergyHandle()

    code = server.main(
        ["--media-mode", "conversation"], TTS_ENV, serve=lambda _s: None, prewarm=prewarm
    )

    assert code == server.EXIT_OK
    assert loads == [1]
    assert server._config().media_mode is MediaMode.CONVERSATION
    captured = capsys.readouterr()
    assert TTS_ENV["SARVAM_API_KEY"] not in captured.out + captured.err
