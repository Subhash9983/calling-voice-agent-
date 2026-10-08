"""Runner <-> activity seams added in WP10 (fake database by default, Atlas with ``-m atlas``).

- an activity-requested end (idle timeout, the time-limit notice) takes the
  runner's single terminal path and completes as ``ended`` with its reason;
- transport lifecycle events (browser left / rejoined) reach the activity's
  listener, while the runner keeps its own reconnect-window handling;
- a failing listener never breaks the runner.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_worker_runner import (
    AGENT,
    BROWSER,
    TIMING,
    WINDOW_MS,
    Rig,
    _result,
    _rig,
    _until,
)
from tests.support.fake_livekit import Decimate

from voice_agent.agent_worker.admission import AdmissionRejectedError, JobAdmission, admit_job
from voice_agent.agent_worker.session_runner import (
    ActivityContext,
    ActivityFactory,
    RunOutcome,
    RunResult,
    WorkerSessionRunner,
)
from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.contracts.transport import TransportEvent, TransportEventKind
from voice_agent.domain.worker_lease import WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.recovery import MongoWorkerRecoveryRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

pytestmark = pytest.mark.asyncio
LONG_AGO = datetime(2026, 9, 27, tzinfo=UTC)


def _runner(rig: Rig, activity: ActivityFactory) -> WorkerSessionRunner:
    def factory(generation: int) -> LiveKitSessionTransport:
        return LiveKitSessionTransport(
            session_id=rig.session_id,
            browser_identity=BROWSER,
            agent_identity=AGENT,
            worker_generation=generation,
            gateway=rig.gateway,
            clock=SystemClock(),
            resampler_factory=Decimate,
            reconnect_window_ms=WINDOW_MS * 10,
        )

    return WorkerSessionRunner(
        rig.stores,
        admission=rig.admission,
        claim=WorkerClaim(worker_instance_id="worker-wp10", livekit_job_id="job-wp10"),
        transport_factory=factory,
        heartbeat_s=0.05,
        media_timing=TIMING,
        activity=activity,
    )


async def _start(rig: Rig, activity: ActivityFactory) -> asyncio.Task[RunResult]:
    task = asyncio.create_task(_runner(rig, activity).run())

    async def connected() -> bool:
        return rig.gateway.connected

    await _until(connected)
    rig.gateway.open_microphone(BROWSER)
    return task


async def test_activity_requested_idle_end_completes_as_ended(backend: Backend) -> None:
    rig = await _rig(backend)

    async def idle_activity(context: ActivityContext) -> None:
        await asyncio.sleep(0.05)
        context.request_end(DisconnectReason.IDLE_TIMEOUT)
        await asyncio.Event().wait()

    task = await _start(rig, idle_activity)
    result = await _result(task)
    raw = await rig.raw()

    assert result == RunResult(RunOutcome.ENDED, DisconnectReason.IDLE_TIMEOUT)
    assert raw["status"] == "ended"
    assert raw["disconnect_reason"] == "idle_timeout"


async def test_speech_activity_sessions_send_the_browser_a_liveness_heartbeat(
    backend: Backend,
) -> None:
    """Regression: a quiet speech-mode session must not look like a lost agent.

    The browser shows "Reconnecting agent" after 5 s without any agent
    message; only the tone/echo check used to send ``va.metrics.v1``.
    """
    rig = await _rig(backend)

    async def quiet(_context: ActivityContext) -> None:
        await asyncio.Event().wait()  # listening; the user has not spoken yet

    task = await _start(rig, quiet)

    async def beating() -> bool:
        return len([s for s in rig.gateway.sent if s.topic == "va.metrics.v1"]) >= 2

    await _until(beating)
    await rig.request_end()
    result = await _result(task)

    beats = [s for s in rig.gateway.sent if s.topic == "va.metrics.v1"]
    assert all(not beat.reliable and beat.destination == BROWSER for beat in beats)
    assert beats[0].body["event_type"] == "transport.quality_updated"
    assert beats[0].body["payload"]["lease_valid_for_ms"] >= 0
    assert result.outcome is RunOutcome.ENDED


async def test_lifecycle_events_reach_the_activity_listener(backend: Backend) -> None:
    rig = await _rig(backend)
    seen: list[TransportEventKind] = []

    def broken(_event: TransportEvent) -> None:
        raise RuntimeError("listener bug")

    async def listening_activity(context: ActivityContext) -> None:
        context.add_lifecycle_listener(broken)
        context.add_lifecycle_listener(lambda event: seen.append(event.kind))
        await asyncio.Event().wait()

    task = await _start(rig, listening_activity)

    async def active() -> bool:
        return (await rig.raw())["status"] == "active"

    await _until(active)
    await asyncio.sleep(0.05)
    rig.gateway.leave(BROWSER)
    await asyncio.sleep(0.05)
    rig.gateway.join(BROWSER)
    await asyncio.sleep(0.05)
    await rig.request_end()
    result = await _result(task)

    assert TransportEventKind.BROWSER_LEFT in seen
    assert TransportEventKind.BROWSER_JOINED in seen
    assert result.outcome is RunOutcome.ENDED


async def _crash_and_authorize(rig: Rig) -> str:
    """Generation 1 claimed long ago, went active, and its lease expired; recovery started."""
    leases = MongoWorkerLeaseRepository(rig.backend.persistence)
    first = await leases.claim_initial(
        rig.session_id,
        WorkerClaim(worker_instance_id="worker-crashed", livekit_job_id="job-crashed"),
        expected_revision=rig.admission.record.state_revision,
        now=LONG_AGO,
    )
    assert first is not None
    worker = MongoWorkerSessionRepository(rig.backend.persistence, clock=SystemClock(), fence=first)
    session = await worker.get(rig.session_id)
    assert session is not None
    await worker.save(session.transition_to(SessionStatus.ACTIVE))
    dispatch_id = str(uuid.uuid4())
    started = await MongoWorkerRecoveryRepository(rig.backend.persistence).start_recovery(
        rig.session_id,
        owner_instance_id="reconciler-test",
        recovery_dispatch_id=dispatch_id,
        now=datetime.now(UTC),
    )
    assert started is not None
    return dispatch_id


async def _recovery_admission(rig: Rig, dispatch_id: str | None) -> JobAdmission:
    record = rig.admission.record
    metadata = encode_dispatch_metadata(
        DispatchLocator(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            agent_config_id=record.agent_config_id,
            environment="development",
            recovery_dispatch_id=dispatch_id,
        )
    )
    return await admit_job(
        metadata=metadata,
        room_name=f"va-rd-{record.session_id}",
        sessions=MongoSessionRecordRepository(rig.backend.persistence),
        configs=MongoAgentConfigRepository(rig.backend.persistence),
        app_env="development",
        now=datetime.now(UTC),
    )


async def test_replacement_worker_claims_the_recovery_and_resumes_listening(
    backend: Backend,
) -> None:
    rig = await _rig(backend)
    dispatch_id = await _crash_and_authorize(rig)
    with pytest.raises(AdmissionRejectedError):
        await _recovery_admission(rig, None)  # an initial job cannot claim during recovery
    with pytest.raises(AdmissionRejectedError):
        await _recovery_admission(rig, str(uuid.uuid4()))  # another dispatch's job
    rig.admission = await _recovery_admission(rig, dispatch_id)
    assert rig.admission.is_recovery

    async def quiet(_context: ActivityContext) -> None:
        await asyncio.Event().wait()

    task = await _start(rig, quiet)

    async def resumed() -> bool:
        return "worker.recovery_claimed" in await rig.event_types()

    await _until(resumed)
    raw = await rig.raw()
    await rig.request_end()
    result = await _result(task)

    assert raw["status"] == "active"
    assert raw["worker_assignment"]["generation"] == 2
    assert raw["agent_activity_state"] == "listening"
    assert "recovery_authorization" not in raw
    assert result.outcome is RunOutcome.ENDED
    assert (await rig.raw())["status"] == "ended"
