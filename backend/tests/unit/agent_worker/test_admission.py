"""Job admission validates the locator against durable state (docs/05 §3; docs/06 §4, §7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tests.support.persistence_builders import make_session, now_ms

from voice_agent.agent_worker.admission import (
    AdmissionRejectedError,
    RejectReason,
    admit_job,
)
from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import (
    SessionRecord,
    TerminationRequester,
    TransportBinding,
)
from voice_agent.persistence.control_plane_memory import InMemorySessionRecordRepository
from voice_agent.provider_registry.media_check_config import media_check_agent_config_document
from voice_agent.provider_registry.mock_config import mock_agent_config_document

ROOM = "va-rd-room-1"
NOW = now_ms()


class Configs:
    def __init__(self, *configs: AgentConfig) -> None:
        self._by_id = {c.agent_config_id: c for c in configs}

    async def get(self, agent_config_id: str) -> AgentConfig | None:
        return self._by_id.get(agent_config_id)


def _config(**overrides: Any) -> AgentConfig:
    return AgentConfig.model_validate(media_check_agent_config_document(**overrides))


def _binding(agent: str | None = "va-agent-1", room: str = ROOM) -> TransportBinding:
    return TransportBinding(
        provider="livekit",
        external_room_id=room,
        external_session_id="AD_1",
        browser_participant_id="va-user-1",
        agent_participant_id=agent,
    )


async def _store(record: SessionRecord) -> InMemorySessionRecordRepository:
    store = InMemorySessionRecordRepository()
    await store.insert(record)
    return store


def _connecting(config: AgentConfig, **binding: Any) -> SessionRecord:
    return make_session(config, now=NOW).bind_transport(_binding(**binding), now=NOW)


def _metadata(record: SessionRecord, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "session_id": record.session_id,
        "correlation_id": record.correlation_id,
        "agent_config_id": record.agent_config_id,
        "environment": "development",
        **overrides,
    }
    return encode_dispatch_metadata(DispatchLocator.model_validate(values))


async def _admit(record: SessionRecord, config: AgentConfig, **kwargs: Any) -> Any:
    return await admit_job(
        metadata=kwargs.pop("metadata", _metadata(record)),
        room_name=kwargs.pop("room_name", ROOM),
        sessions=await _store(record),
        configs=Configs(config),
        app_env=kwargs.pop("app_env", "development"),
        now=kwargs.pop("now", NOW),
    )


@pytest.mark.asyncio
async def test_valid_job_is_admitted_with_the_stored_agent_identity() -> None:
    config = _config()
    record = _connecting(config)

    admission = await _admit(record, config)

    assert admission.agent_identity == "va-agent-1"
    assert admission.browser_identity == "va-user-1"
    assert admission.config == config
    assert admission.locator.session_id == record.session_id


async def _rejected(record: SessionRecord, config: AgentConfig, **kwargs: Any) -> RejectReason:
    with pytest.raises(AdmissionRejectedError) as caught:
        await _admit(record, config, **kwargs)
    return caught.value.reason


@pytest.mark.asyncio
async def test_invalid_metadata_and_environment() -> None:
    config = _config()
    record = _connecting(config)

    assert await _rejected(record, config, metadata="{}") is RejectReason.INVALID_METADATA
    assert await _rejected(record, config, app_env="rd") is RejectReason.ENVIRONMENT_MISMATCH


@pytest.mark.asyncio
async def test_unknown_session() -> None:
    config = _config()
    record = _connecting(config)
    other = make_session(config, now=NOW)

    reason = await _rejected(record, config, metadata=_metadata(other))

    assert reason is RejectReason.SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_locator_must_match_durable_session() -> None:
    config = _config()
    record = _connecting(config)

    reason = await _rejected(record, config, metadata=_metadata(record, correlation_id="forged"))

    assert reason is RejectReason.LOCATOR_MISMATCH


@pytest.mark.asyncio
async def test_only_connecting_unterminated_sessions_are_claimable() -> None:
    config = _config()
    created = make_session(config, now=NOW)
    ending = (
        _connecting(config)
        .request_end(
            client_request_id="00000000-0000-4000-8000-0000000000e1",
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=NOW,
        )
        .record
    )

    assert await _rejected(created, config) is RejectReason.NOT_CLAIMABLE
    assert await _rejected(ending, config) is RejectReason.TERMINATION_REQUESTED


@pytest.mark.asyncio
async def test_duplicate_dispatch_with_a_live_worker_is_rejected() -> None:
    config = _config()
    record = _connecting(config).model_copy(
        update={"worker_lease_expires_at": NOW + timedelta(seconds=10)}
    )

    assert await _rejected(record, config) is RejectReason.WORKER_ALREADY_ASSIGNED


@pytest.mark.asyncio
async def test_maximum_duration_reached() -> None:
    config = _config()
    record = _connecting(config)
    later = NOW + timedelta(milliseconds=record.maximum_session_ms)

    assert await _rejected(record, config, now=later) is RejectReason.MAXIMUM_DURATION_REACHED


@pytest.mark.asyncio
async def test_room_and_agent_identity_must_match() -> None:
    config = _config()

    assert await _rejected(_connecting(config), config, room_name="va-rd-other") is (
        RejectReason.ROOM_MISMATCH
    )
    assert await _rejected(_connecting(config, agent=None), config) is RejectReason.ROOM_MISMATCH


@pytest.mark.asyncio
async def test_configuration_must_be_the_exact_livekit_version() -> None:
    config = _config()
    record = _connecting(config)
    mock = AgentConfig.model_validate(
        mock_agent_config_document(agent_config_id=config.agent_config_id)
    )

    reason = await _rejected(record, mock)

    assert reason is RejectReason.CONFIGURATION_UNUSABLE
