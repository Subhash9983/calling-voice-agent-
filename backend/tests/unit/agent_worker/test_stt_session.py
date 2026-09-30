"""STT-check wiring: config mapping, activity selection, Silero prewarm (offline)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.fake_session_transport import FakeSessionTransport
from tests.support.fake_silero import EnergyHandle
from tests.support.persistence_builders import make_session

from voice_agent.agent_worker import server
from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.entrypoint import WorkerConfig, session_activity, worker_stores
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.session_runner import ActivityContext
from voice_agent.agent_worker.stt_session import (
    SttSessionDeps,
    build_stt_check,
    jittered_backoff,
    rate_card_for,
    stt_stream_config,
    uses_real_stt,
)
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.media_check_config import media_check_agent_config_document
from voice_agent.provider_registry.startup_check import builtin_config_lookup
from voice_agent.provider_registry.stt_check_config import (
    STT_CHECK_AGENT_CONFIG_ID,
    stt_check_agent_config_document,
)
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.stt_adapters.deepgram.sdk_binding import SdkDeepgramConnector

STT_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": STT_CHECK_AGENT_CONFIG_ID,
    "LIVEKIT_URL": "wss://wp7-unit.livekit.cloud",
    "LIVEKIT_API_KEY": "APIwp7unitkey01",
    "LIVEKIT_API_SECRET": "wp7UnitSecretValue0123456789abcdefghijkl",
    "MONGODB_URI": "mongodb+srv://wp7user:wp7pass@cluster-wp7.abc123.mongodb.net/",
    "DEEPGRAM_API_KEY": "dgUnitTestKey0123456789abcdef",
}


FakeHandle = EnergyHandle


def _stt_config() -> AgentConfig:
    return AgentConfig.model_validate(stt_check_agent_config_document())


def _admission(config: AgentConfig) -> JobAdmission:
    return JobAdmission(
        locator=None,  # type: ignore[arg-type]
        record=make_session(config),
        config=config,
        agent_identity="va-agent-unit",
        browser_identity="va-user-unit",
        room_name="va-rd-unit",
    )


def _persistence() -> MongoPersistence:
    database = FakeDatabase()
    return MongoPersistence.from_handles(FakeClient(database), database)


def test_stt_check_config_is_builtin_seeded_and_uses_real_stt() -> None:
    ids = [d["agent_config_id"] for d in builtin_agent_config_documents()]

    assert STT_CHECK_AGENT_CONFIG_ID in ids
    assert builtin_config_lookup(STT_CHECK_AGENT_CONFIG_ID) is not None
    assert uses_real_stt(_stt_config())
    assert not uses_real_stt(AgentConfig.model_validate(media_check_agent_config_document()))


def test_stream_config_maps_the_approved_section_with_empty_keyterms() -> None:
    config = stt_stream_config(_stt_config().stt)

    assert config == SttStreamConfig()
    assert config.keyterms == ()


def test_rate_card_follows_the_configured_version() -> None:
    config = _stt_config()

    card = rate_card_for(config)
    unknown = config.model_copy(update={"cost_rate_card_version": "unknown_card"})

    assert card is not None
    assert card.rate_card_id == "phase0_rate_card_2026_09_26_v1"
    assert rate_card_for(unknown) is None


def test_backoff_is_bounded_by_the_retry_policy() -> None:
    delay = jittered_backoff(RetryPolicy())

    assert 125 <= delay(1) <= 250
    assert 1000 <= delay(5) <= 2000


def _worker_config(mode: MediaMode, *, silero: Any = None) -> WorkerConfig:
    settings = load_bootstrap_configuration(STT_ENV).settings
    return WorkerConfig(settings=settings, media_mode=mode, worker_instance_id="w-7", silero=silero)


def test_activity_selection_requires_stt_mode_config_and_prewarmed_model() -> None:
    persistence = _persistence()
    stt_admission = _admission(_stt_config())
    media_admission = _admission(AgentConfig.model_validate(media_check_agent_config_document()))
    stores = worker_stores(persistence, _worker_config(MediaMode.STT).settings)

    assert (
        session_activity(_worker_config(MediaMode.TONE), stt_admission, persistence, stores) is None
    )
    stt_mode = _worker_config(MediaMode.STT, silero=FakeHandle())
    assert session_activity(stt_mode, media_admission, persistence, stores) is None
    assert (
        session_activity(_worker_config(MediaMode.STT), stt_admission, persistence, stores) is None
    )
    assert session_activity(stt_mode, stt_admission, persistence, stores) is not None


def test_build_uses_the_resolved_credential_and_the_prewarmed_model() -> None:
    admission = _admission(_stt_config())
    keys: list[SecretStr] = []
    deps = SttSessionDeps(
        settings=load_bootstrap_configuration(STT_ENV).settings,
        silero=FakeHandle(),  # type: ignore[arg-type]
        persistence=_persistence(),
        events=None,  # type: ignore[arg-type]
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        connector_factory=lambda key: keys.append(key) or SdkDeepgramConnector(key),  # type: ignore[func-returns-value]
    )
    context = ActivityContext(
        transport=FakeSessionTransport(),
        session_id=admission.record.session_id,
        correlation_id=admission.record.correlation_id,
        worker_generation=1,
        lease_hint=lambda: 1000,
    )

    check = build_stt_check(context, admission, deps)

    assert check is not None
    assert [k.get_secret_value() for k in keys] == [STT_ENV["DEEPGRAM_API_KEY"]]


def test_main_prewarms_silero_once_for_stt_mode(capsys: pytest.CaptureFixture[str]) -> None:
    served: list[Any] = []
    loads: list[int] = []

    def prewarm() -> Any:
        loads.append(1)
        return FakeHandle()

    code = server.main(["--media-mode", "stt"], STT_ENV, serve=served.append, prewarm=prewarm)

    captured = capsys.readouterr()
    assert code == server.EXIT_OK
    assert loads == [1]
    assert server._config().media_mode is MediaMode.STT
    assert server._config().silero is not None
    for secret in (STT_ENV["DEEPGRAM_API_KEY"], STT_ENV["LIVEKIT_API_SECRET"]):
        assert secret not in captured.out + captured.err


def test_main_refuses_stt_config_without_a_deepgram_key(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {k: v for k, v in STT_ENV.items() if k != "DEEPGRAM_API_KEY"}

    code = server.main(["--media-mode", "stt"], environ, serve=lambda _s: None, prewarm=FakeHandle)

    assert code == server.EXIT_REFUSED
    assert '"started": false' in capsys.readouterr().err


def test_tone_mode_does_not_load_the_model() -> None:
    loads: list[int] = []

    code = server.main(
        [],
        {**STT_ENV},
        serve=lambda _s: None,
        prewarm=lambda: loads.append(1) or FakeHandle(),  # type: ignore[func-returns-value]
    )

    assert code == server.EXIT_OK
    assert loads == []
