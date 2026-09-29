"""Worker session runner over the real MongoDB repositories and a fake LiveKit room.

Runs against the in-process fake database by default and Atlas with
``-m atlas``. Covers claim/duplicate-dispatch protection, activation after
browser + microphone verification, fast and durable end paths, reconnect
window expiry, rejoin within the window, unexpected participants, eviction
self-fencing, and the startup barrier.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.fake_livekit import Decimate, FakeGateway
from tests.support.persistence_builders import make_session

from voice_agent.agent_worker.admission import JobAdmission, admit_job
from voice_agent.agent_worker.media_check import MediaTiming
from voice_agent.agent_worker.session_runner import (
    RunOutcome,
    RunResult,
    WorkerSessionRunner,
    WorkerStores,
)
from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.realtime_wire import (
    CONTROL_TOPIC,
    EndRequestedSignal,
    encode_end_requested,
)
from voice_agent.domain.agent_config import AgentConfig, AgentConfigEnvironment
from voice_agent.domain.control_session import TerminationRequester, TransportBinding
from voice_agent.domain.worker_lease import WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.provider_registry.media_check_config import media_check_agent_config_document
from voice_agent.transport_adapters.livekit.gateway import RoomDisconnectCause
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport
from voice_agent.transport_adapters.mock.control import MockTransportControl

pytestmark = pytest.mark.asyncio
BROWSER = "va-user-wp6"
AGENT = "va-agent-wp6"
WINDOW_MS = 150
TIMING = MediaTiming(burst_ms=60, period_ms=40, metrics_interval_s=0.05)


@dataclass
class Rig:
    backend: Backend
    admission: JobAdmission
    stores: WorkerStores
    cleanup: MockTransportControl
    gateway: FakeGateway

    def runner(self, gateway: FakeGateway | None = None, job: str = "job-1") -> WorkerSessionRunner:
        room = gateway or self.gateway

        def factory(generation: int) -> LiveKitSessionTransport:
            return LiveKitSessionTransport(
                session_id=self.session_id,
                browser_identity=BROWSER,
                agent_identity=AGENT,
                worker_generation=generation,
                gateway=room,
                clock=SystemClock(),
                resampler_factory=Decimate,
                reconnect_window_ms=WINDOW_MS,
            )

        return WorkerSessionRunner(
            self.stores,
            admission=self.admission,
            claim=WorkerClaim(worker_instance_id="worker-wp6", livekit_job_id=job),
            transport_factory=factory,
            heartbeat_s=0.05,
            media_timing=TIMING,
        )

    @property
    def session_id(self) -> str:
        return self.admission.record.session_id

    async def raw(self) -> dict[str, Any]:
        raw = await self.backend.database[Collection.VOICE_SESSIONS.value].find_one(
            {"session_id": self.session_id}
        )
        assert raw is not None
        return dict(raw)

    async def event_types(self) -> list[str]:
        rows = (
            await self.backend.database[Collection.SESSION_EVENTS.value]
            .find({"session_id": self.session_id})
            .to_list(length=100)
        )
        return [row["event_type"] for row in sorted(rows, key=lambda r: r["sequence_number"])]

    async def request_end(self, reason: DisconnectReason = DisconnectReason.USER_ENDED) -> None:
        sessions = MongoSessionRecordRepository(self.backend.persistence)
        for _attempt in range(5):
            record = await sessions.get(self.session_id)
            assert record is not None
            decision = record.request_end(
                client_request_id=_uuid(),
                reason=reason,
                requested_by=TerminationRequester.ANONYMOUS_USER,
                now=datetime.now(UTC),
            )
            try:
                await sessions.replace(decision.record, expected_revision=record.state_revision)
                return
            except Exception:  # a concurrent worker write bumped the revision; re-read
                await asyncio.sleep(0.01)
        raise AssertionError("could not record the termination request")


def _uuid() -> str:
    return str(uuid.uuid4())


async def _rig(backend: Backend, **timeouts: int) -> Rig:
    document = media_check_agent_config_document(
        agent_config_id=_uuid(),
        agent_id=_uuid(),
        timeout_policy={
            "browser_join_ms": timeouts.get("browser_join_ms", 3000),
            "maximum_session_ms": timeouts.get("maximum_session_ms", 1_800_000),
        },
    )
    backend.tracker.configs.add(document["agent_config_id"])
    config = MongoAgentConfigRepository(backend.persistence)
    parsed = AgentConfig.model_validate(document)
    await config.insert(parsed)
    record = backend.track_session(make_session(parsed))
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.insert(record)
    binding = TransportBinding(
        provider="livekit",
        external_room_id=f"va-rd-{record.session_id}",
        external_session_id="AD_wp6",
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
    admission = await admit_job(
        metadata=metadata,
        room_name=binding.external_room_id,
        sessions=sessions,
        configs=config,
        app_env="development",
        now=backend.now(),
    )
    cleanup = MockTransportControl()
    context = EventWriteContext(
        environment=AgentConfigEnvironment.DEVELOPMENT, service_version="0.6.0"
    )
    stores = WorkerStores(
        sessions=sessions,
        leases=MongoWorkerLeaseRepository(backend.persistence),
        worker_sessions=lambda token: MongoWorkerSessionRepository(
            backend.persistence, clock=SystemClock(), fence=token
        ),
        events=MongoSessionEventLog(backend.persistence, context=context),
        cleanup=cleanup,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )
    return Rig(backend, admission, stores, cleanup, FakeGateway(present={BROWSER}))


async def _until(check: Callable[[], Awaitable[bool]], attempts: int = 150) -> None:
    for _attempt in range(attempts):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _started(rig: Rig) -> asyncio.Task[RunResult]:
    task = asyncio.create_task(rig.runner().run())

    async def connected() -> bool:
        return rig.gateway.connected

    await _until(connected)
    rig.gateway.open_microphone(BROWSER)

    async def active() -> bool:
        return (await rig.raw())["status"] == "active"

    await _until(active)
    return task


async def _result(task: asyncio.Task[RunResult]) -> RunResult:
    async with asyncio.timeout(5):
        return await task


async def test_fast_signal_end_is_verified_then_terminalizes(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)
    await asyncio.sleep(0.15)  # let the media check play at least one burst

    await rig.request_end()
    rig.gateway.data(None, CONTROL_TOPIC, _signal(rig.session_id), True)
    result = await _result(task)
    raw = await rig.raw()
    types = await rig.event_types()

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.USER_ENDED)
    assert raw["status"] == "ended"
    assert raw["disconnect_reason"] == "user_ended"
    assert "lease_expires_at" not in raw["worker_assignment"]
    assert "session.active" in types
    assert types[-1] == "session.ended"
    assert rig.cleanup.released == [rig.admission.room_name]
    assert rig.gateway.sink.captured  # the test tone reached the publication source
    assert not rig.gateway.connected


def _signal(session_id: str) -> bytes:
    return encode_end_requested(
        EndRequestedSignal.build(
            event_id=_uuid(),
            session_id=session_id,
            correlation_id="corr",
            occurred_at=datetime.now(UTC),
            termination_request_revision=1,
            reason=DisconnectReason.USER_ENDED,
        )
    )


async def test_fast_signal_without_durable_request_is_ignored(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.data(None, CONTROL_TOPIC, _signal(rig.session_id), True)
    await asyncio.sleep(0.2)
    still_active = (await rig.raw())["status"]
    await rig.request_end()
    result = await _result(task)

    assert still_active == "active"
    assert result.outcome is RunOutcome.ENDED


async def test_lost_end_packet_is_recovered_from_the_durable_request(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    await rig.request_end()
    result = await _result(task)

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.USER_ENDED)
    assert (await rig.raw())["status"] == "ended"


async def test_reconnect_window_expiry_ends_with_browser_closed(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.leave(BROWSER)
    result = await _result(task)

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.BROWSER_CLOSED)
    assert (await rig.raw())["disconnect_reason"] == "browser_closed"


async def test_rejoin_within_window_keeps_the_session(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.leave(BROWSER)
    await asyncio.sleep(WINDOW_MS / 3000)
    rig.gateway.join(BROWSER)
    await asyncio.sleep(WINDOW_MS / 500)
    status = (await rig.raw())["status"]
    await rig.request_end()
    result = await _result(task)

    assert status == "active"
    assert result.outcome is RunOutcome.ENDED


async def test_unexpected_participant_fails_the_session(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.join("intruder")
    result = await _result(task)

    assert result == RunResult(RunOutcome.FAILED, DisconnectReason.TRANSPORT_ERROR)
    assert (await rig.raw())["status"] == "failed"
    assert rig.cleanup.released == [rig.admission.room_name]


async def test_eviction_self_fences_without_writing_the_session(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.drop(RoomDisconnectCause.DUPLICATE_IDENTITY)
    result = await _result(task)
    raw = await rig.raw()

    assert result.outcome is RunOutcome.SELF_FENCED
    assert raw["status"] == "active"  # the replacement/reconciler owns what happens next
    assert "worker.self_fenced" in await rig.event_types()
    assert rig.cleanup.released == []


async def test_duplicate_dispatch_loses_the_claim(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    second = await rig.runner(FakeGateway(present={BROWSER}), job="job-2").run()
    await rig.request_end()
    await _result(task)

    assert second.outcome is RunOutcome.REJECTED
    assert (await rig.raw())["worker_assignment"]["livekit_job_id"] == "job-1"


async def test_browser_that_never_joins_fails_the_session(backend: Backend) -> None:
    rig = await _rig(backend, browser_join_ms=200)
    rig.gateway.present.clear()

    result = await _result(asyncio.create_task(rig.runner().run()))

    assert result == RunResult(RunOutcome.FAILED, DisconnectReason.TRANSPORT_ERROR)
    assert (await rig.raw())["status"] == "failed"


async def test_end_before_activation_is_honoured_at_the_startup_barrier(
    backend: Backend,
) -> None:
    rig = await _rig(backend)
    task = asyncio.create_task(rig.runner().run())

    async def claimed() -> bool:
        return "worker_assignment" in await rig.raw() and rig.gateway.connected

    await _until(claimed)
    await rig.request_end()
    rig.gateway.open_microphone(BROWSER)
    result = await _result(task)

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.USER_ENDED)
    assert "session.active" not in await rig.event_types()


async def test_transport_connect_failure_fails_the_session(backend: Backend) -> None:
    rig = await _rig(backend)
    rig.gateway.fail_connect = RuntimeError("provider detail")

    result = await _result(asyncio.create_task(rig.runner().run()))

    assert result == RunResult(RunOutcome.FAILED, DisconnectReason.TRANSPORT_ERROR)
    assert (await rig.raw())["status"] == "failed"


async def test_room_loss_fails_the_session(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    rig.gateway.drop(RoomDisconnectCause.ROOM_DELETED)
    result = await _result(task)

    assert result == RunResult(RunOutcome.FAILED, DisconnectReason.TRANSPORT_ERROR)


async def test_maximum_duration_ends_the_session(backend: Backend) -> None:
    rig = await _rig(backend, maximum_session_ms=1500)
    task = await _started(rig)

    result = await _result(task)

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.MAXIMUM_DURATION)
    assert (await rig.raw())["disconnect_reason"] == "maximum_duration"


async def test_lost_lease_self_fences_the_worker(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    await backend.database[Collection.VOICE_SESSIONS.value].update_one(
        {"session_id": rig.session_id}, {"$inc": {"worker_assignment.writer_epoch": 1}}
    )
    result = await _result(task)

    assert result.outcome is RunOutcome.SELF_FENCED
    assert (await rig.raw())["status"] == "active"  # a fenced worker never writes


async def test_reconciler_style_terminalization_fences_the_worker(backend: Backend) -> None:
    rig = await _rig(backend)
    task = await _started(rig)

    await backend.database[Collection.VOICE_SESSIONS.value].update_one(
        {"session_id": rig.session_id}, {"$set": {"status": "failed", "ended_at": backend.now()}}
    )
    result = await _result(task)

    assert result.outcome is RunOutcome.SELF_FENCED
