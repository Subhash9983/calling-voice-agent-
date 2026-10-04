"""LLM-check wiring and the offline speech -> STT -> GPT-6 Luna -> browser path (WP8).

Synthetic mic audio drives the real VAD/Turn Manager/Deepgram adapter (fake
connector) and the real gate/OpenAI adapter (fake Responses stream); evidence
goes through the real MongoDB repositories over the in-process fake. All keys
are synthetic and passed through an explicit environment mapping, so an
ambient ``OPENAI_API_KEY`` is never read. No network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.fake_openai import FakeResponsesConnector, reply
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport
from tests.support.fake_silero import EnergyHandle
from tests.support.persistence_builders import make_session

from voice_agent.agent_worker import server
from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.entrypoint import WorkerConfig, session_activity, worker_stores
from voice_agent.agent_worker.llm_session import (
    LlmSessionDeps,
    PromptNotApprovedError,
    build_llm_check,
    conversation_setup,
    uses_real_llm,
)
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.session_runner import ActivityContext
from voice_agent.agent_worker.stt_session import SttSessionDeps
from voice_agent.contracts.enums import OperationComponent, OperationStatus, TurnStatus
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.control_plane import EventRecord
from voice_agent.provider_registry.llm_check_config import (
    LLM_CHECK_AGENT_CONFIG_ID,
    llm_check_agent_config_document,
)
from voice_agent.provider_registry.stt_check_config import stt_check_agent_config_document
from voice_agent.security.config_loader import load_bootstrap_configuration

pytestmark = pytest.mark.asyncio
FAKE_OPENAI_KEY = "sk-test-wp8-offline-not-real-0123456789"
LLM_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": LLM_CHECK_AGENT_CONFIG_ID,
    "LIVEKIT_URL": "wss://wp8-unit.livekit.cloud",
    "LIVEKIT_API_KEY": "APIwp8unitkey01",
    "LIVEKIT_API_SECRET": "wp8UnitSecretValue0123456789abcdefghijkl",
    "MONGODB_URI": "mongodb+srv://wp8user:wp8pass@cluster-wp8.abc123.mongodb.net/",
    "DEEPGRAM_API_KEY": "dgUnitTestKey0123456789abcdef",
    "OPENAI_API_KEY": FAKE_OPENAI_KEY,
}


class EventLog:
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()


def _config(document: dict[str, Any] | None = None) -> AgentConfig:
    return AgentConfig.model_validate(document or llm_check_agent_config_document())


def _admission(config: AgentConfig) -> JobAdmission:
    # The fake transport stamps mic frames with its fixed session ID.
    record = make_session(config).model_copy(update={"session_id": SESSION_ID})
    return JobAdmission(
        locator=None,  # type: ignore[arg-type]
        record=record,
        config=config,
        agent_identity="va-agent-unit",
        browser_identity="va-user-unit",
        room_name="va-rd-unit",
    )


def _context(admission: JobAdmission, transport: FakeSessionTransport) -> ActivityContext:
    return ActivityContext(
        transport=transport,
        session_id=admission.record.session_id,
        correlation_id=admission.record.correlation_id,
        worker_generation=1,
        lease_hint=lambda: 1000,
    )


def _persistence() -> MongoPersistence:
    database = FakeDatabase()
    return MongoPersistence.from_handles(FakeClient(database), database)


def _worker_config(mode: MediaMode, *, silero: Any = None) -> WorkerConfig:
    settings = load_bootstrap_configuration(LLM_ENV).settings
    return WorkerConfig(settings=settings, media_mode=mode, worker_instance_id="w-8", silero=silero)


async def test_llm_mode_selects_the_llm_check_only_for_the_openai_configuration() -> None:
    persistence = _persistence()
    llm_admission = _admission(_config())
    stt_admission = _admission(AgentConfig.model_validate(stt_check_agent_config_document()))
    stores = worker_stores(persistence, _worker_config(MediaMode.LLM).settings)
    llm_mode = _worker_config(MediaMode.LLM, silero=EnergyHandle())

    assert uses_real_llm(llm_admission.config)
    assert not uses_real_llm(stt_admission.config)
    assert session_activity(llm_mode, llm_admission, persistence, stores) is not None
    assert session_activity(llm_mode, stt_admission, persistence, stores) is None
    stt_mode = _worker_config(MediaMode.STT, silero=EnergyHandle())
    assert session_activity(stt_mode, llm_admission, persistence, stores) is not None


async def test_setup_comes_from_the_immutable_configuration_and_checks_the_prompt() -> None:
    admission = _admission(_config())
    setup = conversation_setup(_context(admission, FakeSessionTransport()), admission)

    assert setup.prompt_id == "phase0_general_voice_assistant_v1"
    assert setup.prompt_version == 1
    assert setup.max_output_tokens == 250
    assert setup.model == "gpt-6-luna"

    tampered = admission.config.model_copy(
        update={
            "conversation_engine": admission.config.conversation_engine.model_copy(
                update={"system_instruction_version": "2"}
            )
        }
    )
    with pytest.raises(PromptNotApprovedError):
        conversation_setup(_context(admission, FakeSessionTransport()), _admission(tampered))


async def test_speech_to_text_to_streamed_response_offline() -> None:
    admission = _admission(_config())
    transport = FakeSessionTransport()
    persistence = _persistence()
    # The WP5 seed path stores the configuration, as ``seed-configs`` would.
    await seed_agent_configs(persistence, [llm_check_agent_config_document()])
    await MongoSessionRecordRepository(persistence).insert(admission.record)
    keys: list[tuple[str, float]] = []
    openai = FakeResponsesConnector([reply("Namaste Arun! ", "Aapki kya madad karun?")])

    def openai_factory(key: SecretStr, timeout_s: float) -> FakeResponsesConnector:
        keys.append((key.get_secret_value(), timeout_s))
        return openai

    stt = SttSessionDeps(
        settings=load_bootstrap_configuration(LLM_ENV).settings,
        silero=EnergyHandle(),  # type: ignore[arg-type]
        persistence=persistence,
        events=EventLog(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        connector_factory=lambda _key: FakeDeepgramConnector(["मेरा नाम Arun है।"]),
    )
    check = build_llm_check(
        _context(admission, transport), admission, LlmSessionDeps(stt, openai_factory)
    )

    task = asyncio.create_task(check.run())
    await asyncio.wait_for(transport.wait_for_subscribers(2), timeout=1)
    after_speech = await transport.speak(0, 20, 0.8)
    await transport.speak(after_speech, 40, 0.0)
    for _ in range(100):
        if any(s.body["payload"].get("is_final") for s in transport.on("va.response.v1")):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert keys == [(FAKE_OPENAI_KEY, 50.0)]
    assert openai.params[0]["input"] == [{"role": "user", "content": "मेरा नाम Arun है।"}]
    finals = [s.body for s in transport.on("va.response.v1") if s.body["payload"]["is_final"]]
    assert finals[0]["payload"]["text"] == "Namaste Arun! Aapki kya madad karun?"
    assert finals[0]["payload"]["response_completion_status"] == "completed"
    states = [s.body["payload"]["state"] for s in transport.on("va.state.v1")]
    assert "thinking" in states
    write = WriteContext(
        session_id=admission.record.session_id,
        correlation_id=admission.record.correlation_id,
        agent_config_id=admission.record.agent_config_id,
        environment=admission.record.environment,
    )
    operations = await MongoOperationRepository(
        persistence, context=write, clock=SystemClock()
    ).list_for_session(admission.record.session_id)
    conversation = [o for o in operations if o.component is OperationComponent.CONVERSATION_ENGINE]
    assert [o.status for o in conversation] == [OperationStatus.SUCCEEDED]
    turns = await MongoTurnRepository(
        persistence, context=write, clock=SystemClock()
    ).list_for_session(admission.record.session_id)
    assert [t.status for t in turns] == [TurnStatus.COMPLETED]
    assert turns[0].generated_text == "Namaste Arun! Aapki kya madad karun?"


async def test_main_prewarms_silero_for_llm_mode_and_never_prints_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loads: list[int] = []

    def prewarm() -> Any:
        loads.append(1)
        return EnergyHandle()

    code = server.main(["--media-mode", "llm"], LLM_ENV, serve=lambda _s: None, prewarm=prewarm)

    captured = capsys.readouterr()
    assert code == server.EXIT_OK
    assert loads == [1]
    assert server._config().media_mode is MediaMode.LLM
    assert FAKE_OPENAI_KEY not in captured.out + captured.err


async def test_main_refuses_the_llm_config_without_an_openai_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {k: v for k, v in LLM_ENV.items() if k != "OPENAI_API_KEY"}

    code = server.main(
        ["--media-mode", "llm"], environ, serve=lambda _s: None, prewarm=EnergyHandle
    )

    assert code == server.EXIT_REFUSED
    assert '"started": false' in capsys.readouterr().err
