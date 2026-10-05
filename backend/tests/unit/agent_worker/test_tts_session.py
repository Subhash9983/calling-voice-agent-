"""TTS-check wiring and the offline speech -> STT -> GPT-6 Luna -> Bulbul v3 -> audio path (WP9).

Synthetic mic audio drives the real VAD/Turn Manager/Deepgram adapter (fake
connector), the real gate/OpenAI adapter (fake Responses stream), and the
real Sarvam adapter (fake seam); evidence goes through the real MongoDB
repositories over the in-process fake. Keys are synthetic and passed through
an explicit environment mapping, so no ambient key is ever read. No network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.fake_openai import FakeResponsesConnector, reply
from tests.support.fake_sarvam import FakeSarvamConnector
from tests.support.fake_sarvam import reply as speak
from tests.support.fake_session_transport import SESSION_ID, FakeSessionTransport
from tests.support.fake_silero import EnergyHandle
from tests.support.persistence_builders import make_session

from voice_agent.agent_worker import server
from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.entrypoint import WorkerConfig, session_activity, worker_stores
from voice_agent.agent_worker.llm_session import LlmSessionDeps
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.session_runner import ActivityContext
from voice_agent.agent_worker.stt_session import SttSessionDeps
from voice_agent.agent_worker.tts_session import (
    TtsSessionDeps,
    build_tts_check,
    speech_setup,
    uses_real_tts,
    voice_config,
)
from voice_agent.contracts.enums import OperationComponent, OperationStatus, TurnStatus
from voice_agent.contracts.usage import UsageUnit
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.control_plane import EventRecord
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.llm_check_config import llm_check_agent_config_document
from voice_agent.provider_registry.startup_check import builtin_config_lookup
from voice_agent.provider_registry.tts_check_config import (
    TTS_CHECK_AGENT_CONFIG_ID,
    tts_check_agent_config_document,
)
from voice_agent.security.config_loader import load_bootstrap_configuration

pytestmark = pytest.mark.asyncio
FAKE_SARVAM_KEY = "sarvam-test-wp9-offline-not-real-0123456789"
TTS_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": TTS_CHECK_AGENT_CONFIG_ID,
    "LIVEKIT_URL": "wss://wp9-unit.livekit.cloud",
    "LIVEKIT_API_KEY": "APIwp9unitkey01",
    "LIVEKIT_API_SECRET": "wp9UnitSecretValue0123456789abcdefghijkl",
    "MONGODB_URI": "mongodb+srv://wp9user:wp9pass@cluster-wp9.abc123.mongodb.net/",
    "DEEPGRAM_API_KEY": "dgUnitTestKey0123456789abcdef",
    "OPENAI_API_KEY": "sk-test-wp9-offline-not-real-0123456789",
    "SARVAM_API_KEY": FAKE_SARVAM_KEY,
}


class EventLog:
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()


def _config(document: dict[str, Any] | None = None) -> AgentConfig:
    return AgentConfig.model_validate(document or tts_check_agent_config_document())


def _admission(config: AgentConfig) -> JobAdmission:
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


def _worker_config(mode: MediaMode) -> WorkerConfig:
    settings = load_bootstrap_configuration(TTS_ENV).settings
    return WorkerConfig(
        settings=settings, media_mode=mode, worker_instance_id="w-9", silero=EnergyHandle()
    )


async def test_tts_check_config_is_builtin_seedable_and_approved() -> None:
    document = tts_check_agent_config_document()
    config = _config(document)
    persistence = _persistence()

    await seed_agent_configs(persistence, [document])
    stored = await MongoAgentConfigRepository(persistence).get(TTS_CHECK_AGENT_CONFIG_ID)

    assert stored is not None
    assert stored.config_checksum == config.config_checksum
    assert config.tts.provider == "sarvam"
    assert (config.tts.model, config.tts.voice_id) == ("bulbul:v3", "priya")
    assert config.tts.sample_rate_hz == 24_000
    assert any(
        d["agent_config_id"] == TTS_CHECK_AGENT_CONFIG_ID for d in builtin_agent_config_documents()
    )
    assert builtin_config_lookup(TTS_CHECK_AGENT_CONFIG_ID) == document


async def test_tts_mode_selects_the_tts_check_only_for_the_sarvam_configuration() -> None:
    persistence = _persistence()
    tts_admission = _admission(_config())
    llm_admission = _admission(AgentConfig.model_validate(llm_check_agent_config_document()))
    stores = worker_stores(persistence, _worker_config(MediaMode.TTS).settings)
    tts_mode = _worker_config(MediaMode.TTS)

    assert uses_real_tts(tts_admission.config)
    assert not uses_real_tts(llm_admission.config)
    assert session_activity(tts_mode, tts_admission, persistence, stores) is not None
    assert session_activity(tts_mode, llm_admission, persistence, stores) is None
    llm_mode = _worker_config(MediaMode.LLM)
    assert session_activity(llm_mode, tts_admission, persistence, stores) is not None


async def test_voice_and_speech_setup_come_from_the_immutable_configuration() -> None:
    admission = _admission(_config())
    context = _context(admission, FakeSessionTransport())

    voice = voice_config(admission.config.tts)
    setup = speech_setup(context, admission.config)

    assert (voice.provider, voice.model, voice.voice_id) == ("sarvam", "bulbul:v3", "priya")
    assert voice.speaking_rate == Decimal("1.0")
    assert (setup.provider, setup.voice_id, setup.max_queued_segments) == ("sarvam", "priya", 5)
    assert setup.retry == admission.config.retry_policy


async def test_speech_to_spoken_response_offline() -> None:
    admission = _admission(_config())
    transport = FakeSessionTransport()
    persistence = _persistence()
    await seed_agent_configs(persistence, [tts_check_agent_config_document()])
    await MongoSessionRecordRepository(persistence).insert(admission.record)
    keys: list[str] = []
    sarvam = FakeSarvamConnector.with_scripts([speak(3), speak(2)])

    def sarvam_factory(key: SecretStr) -> FakeSarvamConnector:
        keys.append(key.get_secret_value())
        return sarvam

    stt = SttSessionDeps(
        settings=load_bootstrap_configuration(TTS_ENV).settings,
        silero=EnergyHandle(),  # type: ignore[arg-type]
        persistence=persistence,
        events=EventLog(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        connector_factory=lambda _key: FakeDeepgramConnector(["मेरा नाम Arun है।"]),
    )
    openai = FakeResponsesConnector([reply("Namaste Arun! ", "Aapki kya madad karun?")])
    llm = LlmSessionDeps(stt, lambda _key, _timeout: openai)
    check = build_tts_check(
        _context(admission, transport), admission, TtsSessionDeps(llm, sarvam_factory)
    )

    task = asyncio.create_task(check.run())
    await asyncio.wait_for(transport.wait_for_subscribers(2), timeout=1)
    after_speech = await transport.speak(0, 20, 0.8)
    await transport.speak(after_speech, 40, 0.0)
    for _ in range(150):
        if any(s.body["payload"].get("is_final") for s in transport.on("va.response.v1")):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert keys == [FAKE_SARVAM_KEY]
    assert sarvam.opens == 1  # prewarmed at session start, reused for both pieces
    assert sarvam.texts == ["Namaste Arun!", "Aapki kya madad karun?"]
    assert len(transport.published) == 5
    playback = [s.body["payload"]["state"] for s in transport.on("va.playback.v1")]
    assert playback == ["started", "completed", "started", "completed"]
    states = [s.body["payload"]["state"] for s in transport.on("va.state.v1")]
    assert "speaking" in states
    write = WriteContext(
        session_id=admission.record.session_id,
        correlation_id=admission.record.correlation_id,
        agent_config_id=admission.record.agent_config_id,
        environment=admission.record.environment,
    )
    operations = await MongoOperationRepository(
        persistence, context=write, clock=SystemClock()
    ).list_for_session(admission.record.session_id)
    tts = [o for o in operations if o.component is OperationComponent.TTS]
    assert [o.status for o in tts] == [OperationStatus.SUCCEEDED, OperationStatus.SUCCEEDED]
    assert all(o.time_to_first_result_ms is not None for o in tts)
    characters = sum(o.usage.quantity_of(UsageUnit.SYNTHESIZED_CHARACTERS) or 0 for o in tts)
    assert characters == len("Namaste Arun!") + len("Aapki kya madad karun?")
    turns = await MongoTurnRepository(
        persistence, context=write, clock=SystemClock()
    ).list_for_session(admission.record.session_id)
    assert [t.status for t in turns] == [TurnStatus.COMPLETED]
    assert turns[0].spoken_text == "Namaste Arun! Aapki kya madad karun?"


async def test_main_prewarms_silero_for_tts_mode_and_never_prints_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loads: list[int] = []

    def prewarm() -> Any:
        loads.append(1)
        return EnergyHandle()

    code = server.main(["--media-mode", "tts"], TTS_ENV, serve=lambda _s: None, prewarm=prewarm)

    captured = capsys.readouterr()
    assert code == server.EXIT_OK
    assert loads == [1]
    assert server._config().media_mode is MediaMode.TTS
    assert FAKE_SARVAM_KEY not in captured.out + captured.err


async def test_main_refuses_the_tts_config_without_a_sarvam_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {k: v for k, v in TTS_ENV.items() if k != "SARVAM_API_KEY"}

    code = server.main(
        ["--media-mode", "tts"], environ, serve=lambda _s: None, prewarm=EnergyHandle
    )

    assert code == server.EXIT_REFUSED
    assert '"started": false' in capsys.readouterr().err
