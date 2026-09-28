"""In-memory control-plane stores, transports, catalogue, and readiness composition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voice_agent.contracts.enums import SessionStatus
from voice_agent.control_api.readiness import compose_startup_report, probe_readiness
from voice_agent.control_api.runtime import in_memory_stores
from voice_agent.domain.control_session import (
    ComponentSnapshot,
    ProviderSnapshot,
    SessionRecord,
    request_fingerprint,
)
from voice_agent.domain.feedback import FeedbackContent, FeedbackRecord
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionRecordRepository,
)
from voice_agent.ports.control_plane import (
    DuplicateKeyError,
    SessionCursor,
    SessionListQuery,
    StoreUnavailableError,
)
from voice_agent.ports.repositories import RevisionConflictError
from voice_agent.ports.transport_control import TransportAllocation, TransportControlError
from voice_agent.provider_registry.catalog import ApprovedAgentConfigCatalog
from voice_agent.provider_registry.display import UNLISTED_ADAPTER_LABEL, display_label
from voice_agent.provider_registry.mock_config import (
    MOCK_AGENT_CONFIG_ID,
    mock_agent_config_document,
)
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import (
    ComponentReadiness,
    ProcessRole,
    ReadinessComponent,
    ReadinessReport,
    ReadinessStatus,
)
from voice_agent.security.settings import AppEnvironment
from voice_agent.transport_adapters.mock.control import MockTransportControl
from voice_agent.transport_adapters.unavailable import UnavailableTransportControl

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 9, 28, tzinfo=UTC)


def _component(provider: str) -> ComponentSnapshot:
    return ComponentSnapshot(provider=provider, adapter_version="mock-0.1.0")


def record(index: int, *, environment: str = "development", **overrides: object) -> SessionRecord:
    values: dict[str, object] = {
        "session_id": f"00000000-0000-4000-8000-{index:012d}",
        "client_request_id": f"10000000-0000-4000-8000-{index:012d}",
        "create_fingerprint": request_fingerprint({"i": index}),
        "correlation_id": "corr",
        "agent_id": "00000000-0000-4000-8000-00000000a9e1",
        "agent_config_id": MOCK_AGENT_CONFIG_ID,
        "agent_config_version": 1,
        "config_checksum": "sha256:" + "b" * 64,
        "environment": environment,
        "language_mode": "auto",
        "provider_snapshot": ProviderSnapshot(
            transport=_component("mock_transport"),
            stt=_component("mock_stt"),
            conversation_engine=_component("mock_conversation"),
            tts=_component("mock_tts"),
        ),
        "maximum_session_ms": 1_800_000,
        "cost_currency": "USD",
        "cost_rate_card_version": "mock_rate_card_v1",
        "created_at": T0 + timedelta(seconds=index),
        "updated_at": T0,
    }
    values.update(overrides)
    return SessionRecord.model_validate(values)


async def test_session_repository_uniqueness_and_compare_and_set() -> None:
    repo = InMemorySessionRecordRepository()
    first = record(1)
    await repo.insert(first)

    with pytest.raises(DuplicateKeyError):
        await repo.insert(record(1))
    with pytest.raises(DuplicateKeyError):
        await repo.insert(record(2, client_request_id=first.client_request_id))
    await repo.replace(first.model_copy(update={"state_revision": 1}), expected_revision=0)
    with pytest.raises(RevisionConflictError):
        await repo.replace(first, expected_revision=0)
    with pytest.raises(RevisionConflictError):
        await repo.replace(record(9), expected_revision=0)
    assert await repo.get_by_client_request_id(first.client_request_id) is not None
    assert await repo.get_by_client_request_id("missing") is None


async def test_session_repository_list_filters_and_cursor() -> None:
    repo = InMemorySessionRecordRepository()
    for index in range(1, 5):
        await repo.insert(record(index))
    await repo.insert(record(5, environment="rd"))

    newest = await repo.list_page(SessionListQuery(environment="development", limit=2))
    after = SessionCursor(created_at=newest[-1].created_at, session_id=newest[-1].session_id)
    older = await repo.list_page(SessionListQuery(environment="development", limit=5, after=after))
    active = await repo.list_page(
        SessionListQuery(environment="development", limit=5, status=SessionStatus.ACTIVE)
    )

    assert [r.session_id[-1] for r in newest] == ["4", "3"]
    assert [r.session_id[-1] for r in older] == ["2", "1"]
    assert active == []


async def test_unavailable_store_raises_store_unavailable() -> None:
    repo = InMemorySessionRecordRepository()
    repo.available = False
    feedback = InMemoryFeedbackRepository()
    feedback.available = False

    with pytest.raises(StoreUnavailableError):
        await repo.ping()
    with pytest.raises(StoreUnavailableError):
        await feedback.get_by_client_submission_id("x")


async def test_feedback_repository_uniqueness() -> None:
    repo = InMemoryFeedbackRepository()
    content = FeedbackContent.model_validate(
        {"target_type": "session", "aspects": ["overall"], "thumb": "up"}
    )
    item = FeedbackRecord(
        feedback_id="00000000-0000-4000-8000-000000000f01",
        client_submission_id="00000000-0000-4000-8000-000000000f02",
        fingerprint=request_fingerprint({"a": 1}),
        session_id="00000000-0000-4000-8000-000000000001",
        agent_config_id=MOCK_AGENT_CONFIG_ID,
        correlation_id="corr",
        environment="development",
        content=content,
        created_at=T0,
    )
    await repo.insert(item)

    with pytest.raises(DuplicateKeyError):
        await repo.insert(item)


async def test_timeline_event_append_is_idempotent_by_event_id() -> None:
    from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType
    from voice_agent.ports.control_plane import EventQuery, EventRecord

    stores = in_memory_stores()
    envelope = EventEnvelope(
        event_id="00000000-0000-4000-8000-00000000e001",
        event_type=EventType.SESSION_CREATED,
        occurred_at=T0,
        session_id="00000000-0000-4000-8000-000000000001",
        correlation_id="corr",
        component="control_api",
        producer_service="control_api",
        visibility="browser_safe",
    )
    await stores.events.append(EventRecord(envelope, EventSeverity.INFO, T0))
    await stores.events.append(EventRecord(envelope, EventSeverity.INFO, T0))

    events = await stores.timeline.list_events(EventQuery(session_id=envelope.session_id, limit=10))
    assert [e.envelope.sequence_number for e in events] == [1]


async def test_mock_and_unavailable_transport_controls() -> None:
    mock = MockTransportControl()
    allocation = await mock.prepare_session(session_id="s", agent_name="phase0-voice-agent")
    credential = await mock.issue_join_token(allocation, now=T0)
    await mock.release_session(allocation)
    unavailable = UnavailableTransportControl("livekit")
    stub = TransportAllocation(
        provider="livekit", room_name="r", participant_identity="p", dispatch_id=None
    )

    assert credential.expires_at == T0 + timedelta(minutes=10)
    assert credential.token not in repr(credential)
    assert mock.released == [allocation.room_name]
    assert not unavailable.is_available
    assert unavailable.public_url == ""
    for call in (
        unavailable.prepare_session(session_id="s", agent_name="a"),
        unavailable.issue_join_token(stub, now=T0),
        unavailable.release_session(stub),
    ):
        with pytest.raises(TransportControlError):
            await call


async def test_catalogue_keeps_only_approved_active_configurations() -> None:
    tampered = {**mock_agent_config_document(), "config_checksum": "sha256:" + "0" * 64}
    tampered["agent_config_id"] = "00000000-0000-4000-8000-00000000c0f9"
    documents = [mock_agent_config_document(), tampered, {"agent_config_id": 5}]

    development = ApprovedAgentConfigCatalog(documents, app_env=AppEnvironment.DEVELOPMENT)
    rd = ApprovedAgentConfigCatalog(documents, app_env=AppEnvironment.RD)

    assert [c.agent_config_id for c in await development.list_active("development")] == [
        MOCK_AGENT_CONFIG_ID
    ]
    assert await development.get_active(tampered["agent_config_id"]) is None
    assert await rd.list_active("rd") == []


async def test_display_labels() -> None:
    assert display_label("stt", "deepgram") == "Deepgram Nova-3"
    assert display_label("tts", "sarvam") == "Sarvam Bulbul v3"
    assert display_label("tts", "unknown") == UNLISTED_ADAPTER_LABEL


def _report(*statuses: tuple[ReadinessComponent, ReadinessStatus, ConfigReason]) -> ReadinessReport:
    return ReadinessReport(
        ProcessRole.CONTROL_API, tuple(ComponentReadiness(*item) for item in statuses)
    )


async def test_readiness_composition_marks_missing_dependencies() -> None:
    ready = ReadinessStatus.READY
    report = _report(
        (ReadinessComponent.PERSISTENCE, ready, ConfigReason.IN_MEMORY_PERSISTENCE),
        (ReadinessComponent.TRANSPORT, ready, ConfigReason.MOCK_ADAPTER),
    )

    composed = compose_startup_report(report, stores_available=False, transport_available=False)
    unchanged = compose_startup_report(report, stores_available=True, transport_available=True)

    assert composed.reasons == (
        ConfigReason.DEPENDENCY_UNAVAILABLE,
        ConfigReason.DEPENDENCY_UNAVAILABLE,
    )
    assert unchanged == report
    assert await probe_readiness(report, None, timeout_s=0.1) == report


async def test_probe_times_out_safely() -> None:
    import asyncio

    class SlowSessions(InMemorySessionRecordRepository):
        async def ping(self) -> None:
            await asyncio.sleep(1)

    report = _report(
        (ReadinessComponent.PERSISTENCE, ReadinessStatus.READY, ConfigReason.IN_MEMORY_PERSISTENCE)
    )

    probed = await probe_readiness(report, SlowSessions(), timeout_s=0.01)

    assert not probed.ready
