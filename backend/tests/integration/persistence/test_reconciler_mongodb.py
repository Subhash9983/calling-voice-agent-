"""Reconciler against ``voice_sessions`` (fake and ``-m atlas``): fencing and terminalization.

docs/05 §4, §21: a crashed worker's expired lease is fenced (``writer_epoch``
increment) before the session is failed, so a late write from the old worker
is rejected; the agent identity round-trips through the stored transport.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.integration.control_api.conftest import ApiFactory
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV, _seed

from voice_agent.contracts.enums import AgentActivityState, DisconnectReason, SessionStatus
from voice_agent.control_api.reconciler import SessionReconciler
from voice_agent.domain.control_session import SessionRecord, TransportBinding
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.session_reconcile import ReconcileAction
from voice_agent.domain.worker_lease import LeaseToken, WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.ports.persistence import WriterFencedError
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio
CLAIM = WorkerClaim(worker_instance_id="worker-wp6-test", livekit_job_id="job-wp6-test")
# Long before real server time, so the lease is expired for ``$$NOW`` and for
# the reconciler clock (advanced to real time in the test).
LONG_AGO = datetime(2026, 9, 27, tzinfo=UTC)


async def _connecting(backend: Backend) -> SessionRecord:
    record = await backend.session()
    binding = TransportBinding(
        provider=record.provider_snapshot.transport.provider,
        external_room_id=f"va-rd-{record.session_id}",
        external_session_id="AD_test",
        browser_participant_id="va-user-test",
        agent_participant_id="va-agent-test",
    )
    bound = record.bind_transport(binding, now=backend.now())
    await MongoSessionRecordRepository(backend.persistence).replace(bound, expected_revision=0)
    return bound


async def _crashed_active_worker(backend: Backend) -> tuple[SessionRecord, LeaseToken]:
    record = await _connecting(backend)
    token = await MongoWorkerLeaseRepository(backend.persistence).claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=LONG_AGO
    )
    assert token is not None
    worker = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=token)
    session = await worker.get(record.session_id)
    assert session is not None
    await worker.save(session.transition_to(SessionStatus.ACTIVE))
    return record, token


async def test_agent_identity_and_lease_round_trip(backend: Backend) -> None:
    record, token = await _crashed_active_worker(backend)

    stored = await MongoSessionRecordRepository(backend.persistence).get(record.session_id)

    assert stored is not None
    assert stored.transport is not None
    assert stored.transport.agent_participant_id == "va-agent-test"
    assert stored.worker_lease_expires_at == token.lease_expires_at


async def test_crashed_worker_is_fenced_then_failed(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, token = await _crashed_active_worker(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        # Align the harness's manual clock with real time (after the session was created).
        api.clock.advance(int((backend.now() - api.clock.utc_now()).total_seconds() * 1000) + 1000)
        action = await SessionReconciler(api.runtime).reconcile_session(
            record.session_id, writer_epoch=token.writer_epoch
        )
    if not backend.persistence.is_open:
        backend.persistence.open()
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": record.session_id}
    )
    late_worker = MongoWorkerSessionRepository(
        backend.persistence, clock=SystemClock(), fence=token
    )
    stale = VoiceSession(
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        status=SessionStatus.ACTIVE,
        agent_activity_state=AgentActivityState.LISTENING,
        state_revision=raw["state_revision"] if raw else 0,
    )

    assert action is ReconcileAction.FAIL_WORKER_LOST
    assert raw is not None
    assert raw["status"] == "failed"
    assert raw["disconnect_reason"] == DisconnectReason.TRANSPORT_ERROR.value
    assert raw["worker_assignment"]["writer_epoch"] == token.writer_epoch + 1
    assert "next_reconcile_at" not in raw
    with pytest.raises(WriterFencedError):
        await late_worker.save(stale)
