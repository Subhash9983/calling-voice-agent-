"""WP7 STT check through the worker runner, real MongoDB repositories, and a fake room.

Browser microphone PCM (48 kHz) -> LiveKit session transport (single 16 kHz
resample, fan-out) -> VAD + Turn Manager and the Deepgram adapter (scripted
fake connector) -> durable turn, ``provider_operations`` and ``cost_entries``
evidence -> ``va.transcript.v1`` / ``va.state.v1`` to the browser. Runs on the
in-process fake database (with the approved validators) by default and on
Atlas with ``-m atlas``. No provider is called and no audio is stored.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_livekit import Decimate, FakeGateway
from tests.support.fake_silero import EnergyHandle
from tests.support.persistence_builders import make_session

from voice_agent.agent_worker.admission import JobAdmission, admit_job
from voice_agent.agent_worker.session_runner import RunOutcome, WorkerSessionRunner, WorkerStores
from voice_agent.agent_worker.stt_session import SttSessionDeps, stt_activity
from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.domain.agent_config import AgentConfig, AgentConfigEnvironment
from voice_agent.domain.control_session import TerminationRequester, TransportBinding
from voice_agent.domain.worker_lease import WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.provider_registry.stt_check_config import stt_check_agent_config_document
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport
from voice_agent.transport_adapters.mock.control import MockTransportControl

pytestmark = pytest.mark.asyncio
BROWSER = "va-user-wp7"
AGENT = "va-agent-wp7"
TRANSCRIPT = "नमस्ते, मेरा नाम Arun है।"
UNIT_ENV = {"DEEPGRAM_API_KEY": "dgUnitTestKey0123456789abcdef"}


def _uuid() -> str:
    return str(uuid.uuid4())


async def _admission(backend: Backend) -> JobAdmission:
    document = stt_check_agent_config_document(agent_config_id=_uuid(), agent_id=_uuid())
    backend.tracker.configs.add(document["agent_config_id"])
    configs = MongoAgentConfigRepository(backend.persistence)
    parsed = AgentConfig.model_validate(document)
    await configs.insert(parsed)
    record = backend.track_session(make_session(parsed))
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.insert(record)
    binding = TransportBinding(
        provider="livekit",
        external_room_id=f"va-rd-{record.session_id}",
        external_session_id="AD_wp7",
        browser_participant_id=BROWSER,
        agent_participant_id=AGENT,
    )
    bound = record.bind_transport(binding, now=backend.now())
    await sessions.replace(bound, expected_revision=record.state_revision)
    metadata = encode_dispatch_metadata(
        DispatchLocator(
            session_id=bound.session_id,
            correlation_id=bound.correlation_id,
            agent_config_id=bound.agent_config_id,
            environment="development",
        )
    )
    return await admit_job(
        metadata=metadata,
        room_name=binding.external_room_id,
        sessions=sessions,
        configs=configs,
        app_env="development",
        now=backend.now(),
    )


def _stores(backend: Backend) -> WorkerStores:
    context = EventWriteContext(
        environment=AgentConfigEnvironment.DEVELOPMENT, service_version="0.7.0"
    )
    return WorkerStores(
        sessions=MongoSessionRecordRepository(backend.persistence),
        leases=MongoWorkerLeaseRepository(backend.persistence),
        worker_sessions=lambda token: MongoWorkerSessionRepository(
            backend.persistence, clock=SystemClock(), fence=token
        ),
        events=MongoSessionEventLog(backend.persistence, context=context),
        cleanup=MockTransportControl(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )


async def _until(check: Any, attempts: int = 200) -> None:
    for _attempt in range(attempts):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _end(backend: Backend, session_id: str) -> None:
    sessions = MongoSessionRecordRepository(backend.persistence)
    for _attempt in range(5):
        record = await sessions.get(session_id)
        assert record is not None
        decision = record.request_end(
            client_request_id=_uuid(),
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=datetime.now(UTC),
        )
        try:
            await sessions.replace(decision.record, expected_revision=record.state_revision)
            return
        except Exception:  # a concurrent worker write bumped the revision; re-read
            await asyncio.sleep(0.01)
    raise AssertionError("could not record the termination request")


async def _rows(backend: Backend, collection: Collection, session_id: str) -> list[dict[str, Any]]:
    cursor = backend.database[collection.value].find({"session_id": session_id})
    return [dict(row) for row in await cursor.to_list(length=100)]


async def test_stt_check_persists_transcript_operation_usage_and_cost(backend: Backend) -> None:
    admission = await _admission(backend)
    session_id = admission.record.session_id
    stores = _stores(backend)
    gateway = FakeGateway(present={BROWSER})
    connector = FakeDeepgramConnector([TRANSCRIPT])
    deps = SttSessionDeps(
        settings=load_bootstrap_configuration(UNIT_ENV).settings,
        silero=EnergyHandle(),  # type: ignore[arg-type]
        persistence=backend.persistence,
        events=stores.events,
        clock=stores.clock,
        ids=stores.ids,
        connector_factory=lambda _key: connector,
    )

    def transport(generation: int) -> LiveKitSessionTransport:
        return LiveKitSessionTransport(
            session_id=session_id,
            browser_identity=BROWSER,
            agent_identity=AGENT,
            worker_generation=generation,
            gateway=gateway,
            clock=SystemClock(),
            resampler_factory=Decimate,
        )

    runner = WorkerSessionRunner(
        stores,
        admission=admission,
        claim=WorkerClaim(worker_instance_id="worker-wp7", livekit_job_id="job-wp7"),
        transport_factory=transport,
        heartbeat_s=0.05,
        activity=stt_activity(admission, deps),
    )
    task = asyncio.create_task(runner.run())

    async def connected() -> bool:
        return gateway.connected

    await _until(connected)
    gateway.open_microphone(BROWSER)

    async def stream_open() -> bool:
        return bool(connector.connections)

    await _until(stream_open)
    await asyncio.sleep(0.05)
    gateway.speak(frames=20, level=8000)  # 400 ms of "speech"
    gateway.speak(frames=40, level=0)  # 800 ms of silence: past the 700 ms endpoint

    async def transcribed() -> bool:
        return any(sent.topic == "va.transcript.v1" for sent in gateway.sent)

    await _until(transcribed)
    await _end(backend, session_id)
    async with asyncio.timeout(10):
        result = await task

    assert result.outcome is RunOutcome.ENDED
    finals = [
        json.loads(s.payload) for s in gateway.sent if s.topic == "va.transcript.v1" and s.reliable
    ]
    assert [f["payload"] for f in finals] == [{"text": TRANSCRIPT, "is_final": True}]
    assert finals[0]["turn_id"] is not None
    assert finals[0]["sequence_number"] >= 1

    turns = await _rows(backend, Collection.CONVERSATION_TURNS, session_id)
    assert len(turns) == 1
    assert turns[0]["user_input"]["final_transcript"] == TRANSCRIPT
    assert turns[0]["status"] == "abandoned"  # no conversation engine in the STT check

    operations = await _rows(backend, Collection.PROVIDER_OPERATIONS, session_id)
    assert len(operations) == 1
    stream = operations[0]
    assert stream["status"] == "succeeded"
    assert stream["operation_type"] == "stt_stream"
    assert stream["provider_identity"]["provider"] == "deepgram"
    assert stream["provider_identity"]["model"] == "nova-3"
    units = {item["unit"]: item for item in stream["usage"]["items"]}
    assert units["transcribed_audio_seconds"]["source"] == "provider_reported"
    assert stream["total_duration_ms"] >= 0
    assert "started_at" in stream

    costs = await _rows(backend, Collection.COST_ENTRIES, session_id)
    assert {row["scope"] for row in costs} == {"operation", "session"}
    total = await MongoCostEntryStore(backend.persistence).session_charge_total(session_id)
    assert total is not None
    assert total > Decimal(0)
    assert all(
        row["rate"]["rate_card_version"] == "phase0_rate_card_2026_09_26_v1" for row in costs
    )

    events = await _rows(backend, Collection.SESSION_EVENTS, session_id)
    ended = [e for e in events if e["event_type"] == "user.speech_ended"]
    # 700 ms after the last speech frame, at 20 ms frame-tick granularity (never 1,250 ms).
    assert ended
    assert 700 <= ended[0]["payload"]["endpoint_delay_ms"] < 720
    assert any(e["event_type"] == "stt.turn_finalized" for e in events)
    assert not any("text" in e.get("payload", {}) for e in events)  # no transcript in events
