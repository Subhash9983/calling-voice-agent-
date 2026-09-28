"""Worker lease compare-and-set and reconciliation scans (docs/02 §6; docs/05 §4-§6, §21)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import connecting, new_id

from voice_agent.contracts.enums import AgentActivityState, DisconnectReason, SessionStatus
from voice_agent.domain.control_session import SessionRecord, TerminationRequester
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.worker_lease import LEASE_DURATION_MS, WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.leases import (
    MongoSessionReconciliationRepository,
    MongoWorkerLeaseRepository,
)
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.ports.persistence import ReferenceNotFoundError, WriterFencedError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio
CLAIM = WorkerClaim(worker_instance_id="worker-wp5-test", livekit_job_id="job-wp5-test")


async def _connecting(backend: Backend) -> SessionRecord:
    record = connecting(await backend.session())
    await MongoSessionRecordRepository(backend.persistence).replace(record, expected_revision=0)
    return record


async def _raw(backend: Backend, session_id: str) -> dict:  # type: ignore[type-arg]
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": session_id}
    )
    assert raw is not None
    return raw


async def test_initial_claim_records_generation_one_assignment(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)

    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=backend.now()
    )
    duplicate = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision + 1, now=backend.now()
    )
    raw = await _raw(backend, record.session_id)

    assert token is not None
    assert (token.generation, token.writer_epoch, token.lease_revision) == (1, 1, 1)
    assert duplicate is None  # an unexpired assignment blocks a second claim
    assert raw["state_revision"] == record.state_revision + 1
    assert raw["next_reconcile_at"] == min(raw["connect_deadline_at"], token.lease_expires_at)


async def test_claim_rejects_stale_revision_or_termination(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)

    assert (
        await leases.claim_initial(record.session_id, CLAIM, expected_revision=0, now=backend.now())
        is None
    )
    ended = record.request_end(
        client_request_id=new_id(),
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=backend.now(),
    )
    await MongoSessionRecordRepository(backend.persistence).replace(
        ended.record, expected_revision=record.state_revision
    )
    assert (
        await leases.claim_initial(
            record.session_id,
            CLAIM,
            expected_revision=ended.record.state_revision,
            now=backend.now(),
        )
        is None
    )


async def test_heartbeat_renews_lease_without_touching_business_state(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=backend.now()
    )
    assert token is not None
    before = await _raw(backend, record.session_id)

    renewed = await leases.renew_lease(token, now=backend.now())
    after = await _raw(backend, record.session_id)

    assert renewed is not None
    assert renewed.lease_revision == token.lease_revision + 1
    assert after["state_revision"] == before["state_revision"]
    assert after["updated_at"] == before["updated_at"]
    assert "last_activity_at" not in after
    assert await leases.renew_lease(token, now=backend.now()) is None  # stale lease_revision


async def test_concurrent_heartbeats_and_business_writes_never_conflict(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    sessions = MongoSessionRecordRepository(backend.persistence)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=backend.now()
    )
    assert token is not None
    claimed = await sessions.get(record.session_id)
    assert claimed is not None
    ended = claimed.request_end(
        client_request_id=new_id(),
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=backend.now(),
    )

    renewed, replaced = await asyncio.gather(
        leases.renew_lease(token, now=backend.now()),
        sessions.replace(ended.record, expected_revision=claimed.state_revision),
    )
    after = await _raw(backend, record.session_id)

    assert renewed is not None
    assert replaced is None
    assert after["state_revision"] == ended.record.state_revision
    assert after["worker_assignment"]["lease_revision"] == renewed.lease_revision
    assert after["status"] == "ending"


async def test_expired_lease_cannot_renew_and_is_due_for_reconciliation(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    past = backend.now() - timedelta(milliseconds=LEASE_DURATION_MS + 5_000)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=past
    )
    assert token is not None
    scans = MongoSessionReconciliationRepository(backend.persistence)

    assert await leases.renew_lease(token, now=backend.now()) is None
    lease_due = await scans.list_lease_due("development", now=backend.now(), limit=100)
    reconcile_due = await scans.list_reconcile_due("development", now=backend.now(), limit=100)

    assert record.session_id in {c.session_id for c in lease_due}
    assert record.session_id in {c.session_id for c in reconcile_due}


async def test_reconciler_fence_invalidates_the_old_writer(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    past = backend.now() - timedelta(milliseconds=LEASE_DURATION_MS + 5_000)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=past
    )
    assert token is not None

    epoch = await leases.fence_expired(
        record.session_id, expected_writer_epoch=token.writer_epoch, now=backend.now()
    )
    worker = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=token)
    session = VoiceSession(
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        status=SessionStatus.ACTIVE,
        state_revision=9,
    )

    assert epoch == token.writer_epoch + 1
    assert await leases.release(token, now=backend.now()) is False
    with pytest.raises(WriterFencedError):
        await worker.save(session)


async def test_release_removes_the_lease_from_the_due_index(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=backend.now()
    )
    assert token is not None

    assert await leases.release(token, now=backend.now()) is True
    raw = await _raw(backend, record.session_id)

    assert "lease_expires_at" not in raw["worker_assignment"]
    assert raw["worker_assignment"]["released_at"] is not None
    assert raw["next_reconcile_at"] == raw["connect_deadline_at"]


async def test_worker_session_writes_are_fenced_and_revision_checked(backend: Backend) -> None:
    record = await _connecting(backend)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    token = await leases.claim_initial(
        record.session_id, CLAIM, expected_revision=record.state_revision, now=backend.now()
    )
    assert token is not None
    worker = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=token)
    current = await worker.get(record.session_id)
    assert current is not None

    active = current.transition_to(SessionStatus.ACTIVE)
    await worker.save(active)
    listening = await worker.get(record.session_id)
    await worker.save(active)  # idempotent re-save of the same revision
    with pytest.raises(RevisionConflictError):
        await worker.save(current)
    with pytest.raises(ReferenceNotFoundError):
        await worker.save(active.model_copy(update={"session_id": new_id()}))
    raw = await _raw(backend, record.session_id)

    assert listening is not None
    assert listening.status is SessionStatus.ACTIVE
    assert listening.agent_activity_state is AgentActivityState.LISTENING
    assert "connect_deadline_at" not in raw
    assert raw["next_reconcile_at"] == min(
        raw["maximum_duration_deadline_at"], raw["worker_assignment"]["lease_expires_at"]
    )


async def test_worker_terminal_save_sets_the_retention_anchor(backend: Backend) -> None:
    record = await _connecting(backend)
    worker = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=None)
    current = await worker.get(record.session_id)
    assert current is not None
    failed = current.transition_to(
        SessionStatus.FAILED, disconnect_reason=DisconnectReason.TRANSPORT_ERROR
    )

    await worker.save(failed)
    await worker.save(failed)  # a terminal transition is written once; replay is a no-op
    raw = await _raw(backend, record.session_id)

    assert raw["status"] == "failed"
    assert raw["expires_at"] == raw["ended_at"] + timedelta(days=30)
    assert "next_reconcile_at" not in raw
