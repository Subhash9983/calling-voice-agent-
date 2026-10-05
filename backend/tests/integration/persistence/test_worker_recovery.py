"""Worker-crash recovery over ``voice_sessions`` (fake database; Atlas with ``-m atlas``).

docs/05 §21 (Decision 067), WP10: at most one higher-generation recovery;
the fence, ownership lease/takeover, the replacement claim (matching
dispatch ID, unexpired deadline, no termination request), exhausted recovery
to ``failed``, and the reconciler's end-to-end pass with a recovery-capable
transport.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.control_api.conftest import ApiFactory
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV, _seed
from tests.support.persistence_builders import make_turn, new_id, write_context
from tests.support.recovering_transport import RecoveringMockTransport

from voice_agent.contracts.enums import (
    AgentActivityState,
    DisconnectReason,
    SessionStatus,
    TurnStatus,
)
from voice_agent.control_api.reconciler import SessionReconciler
from voice_agent.domain.control_session import (
    SessionRecord,
    TerminationRequester,
    TransportBinding,
)
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.session_reconcile import ReconcileAction
from voice_agent.domain.worker_lease import LeaseToken, WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.recovery import MongoWorkerRecoveryRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import MongoTurnRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.ports.persistence import WriterFencedError
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio
FIRST = WorkerClaim(worker_instance_id="worker-wp10-a", livekit_job_id="job-wp10-a")
REPLACEMENT = WorkerClaim(worker_instance_id="worker-wp10-b", livekit_job_id="job-wp10-b")
LONG_AGO = datetime(2026, 9, 27, tzinfo=UTC)
OWNER = "reconciler-test-owner"


async def _crashed(backend: Backend) -> tuple[SessionRecord, LeaseToken]:
    """An ``active`` session whose generation-1 worker lease expired long ago."""
    record = await backend.session()
    binding = TransportBinding(
        provider="livekit",
        external_room_id=f"va-rd-{record.session_id}",
        external_session_id="AD_wp10",
        browser_participant_id="va-user-wp10",
        agent_participant_id="va-agent-wp10",
    )
    bound = record.bind_transport(binding, now=backend.now())
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.replace(bound, expected_revision=0)
    token = await MongoWorkerLeaseRepository(backend.persistence).claim_initial(
        bound.session_id, FIRST, expected_revision=bound.state_revision, now=LONG_AGO
    )
    assert token is not None
    worker = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=token)
    session = await worker.get(bound.session_id)
    assert session is not None
    await worker.save(session.transition_to(SessionStatus.ACTIVE))
    return bound, token


async def _raw(backend: Backend, session_id: str) -> dict:  # type: ignore[type-arg]
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": session_id}
    )
    assert raw is not None
    return dict(raw)


def _real_now() -> datetime:
    return datetime.now(UTC)


async def test_start_recovery_fences_counts_and_authorizes_once(backend: Backend) -> None:
    record, token = await _crashed(backend)
    recovery = MongoWorkerRecoveryRepository(backend.persistence)
    dispatch_id = new_id()

    authorization = await recovery.start_recovery(
        record.session_id,
        owner_instance_id=OWNER,
        recovery_dispatch_id=dispatch_id,
        now=_real_now(),
    )
    again = await recovery.start_recovery(
        record.session_id, owner_instance_id=OWNER, recovery_dispatch_id=new_id(), now=_real_now()
    )
    raw = await _raw(backend, record.session_id)
    stored = await MongoSessionRecordRepository(backend.persistence).get(record.session_id)

    assert authorization is not None
    assert again is None  # one authorization at a time, at most one recovery
    assert authorization.recovery_dispatch_id == dispatch_id
    assert authorization.owner_generation == 1
    assert authorization.writer_epoch == token.writer_epoch + 1
    assert raw["worker_assignment"]["writer_epoch"] == token.writer_epoch + 1
    assert raw["worker_recovery_count"] == 1
    assert raw["agent_activity_state"] == AgentActivityState.RECOVERING.value
    assert raw["next_reconcile_at"] == authorization.expires_at
    assert stored is not None
    assert stored.recovery_authorization == authorization
    late = MongoWorkerSessionRepository(backend.persistence, clock=SystemClock(), fence=token)
    stale = VoiceSession(
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        status=SessionStatus.ACTIVE,
        state_revision=raw["state_revision"],
    )
    with pytest.raises(WriterFencedError):
        await late.save(stale)


async def test_replacement_claim_needs_the_matching_dispatch_and_unsets_the_authorization(
    backend: Backend,
) -> None:
    record, token = await _crashed(backend)
    recovery = MongoWorkerRecoveryRepository(backend.persistence)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    dispatch_id = new_id()
    await recovery.start_recovery(
        record.session_id,
        owner_instance_id=OWNER,
        recovery_dispatch_id=dispatch_id,
        now=_real_now(),
    )

    wrong = await leases.claim_recovery(
        record.session_id, REPLACEMENT, recovery_dispatch_id=new_id(), now=_real_now()
    )
    claimed = await leases.claim_recovery(
        record.session_id, REPLACEMENT, recovery_dispatch_id=dispatch_id, now=_real_now()
    )
    duplicate = await leases.claim_recovery(
        record.session_id, REPLACEMENT, recovery_dispatch_id=dispatch_id, now=_real_now()
    )
    raw = await _raw(backend, record.session_id)

    assert wrong is None
    assert claimed is not None
    assert claimed.generation == token.generation + 1
    assert claimed.writer_epoch == token.writer_epoch + 2
    assert duplicate is None  # the authorization is gone after one successful claim
    assert "recovery_authorization" not in raw
    assert raw["agent_activity_state"] == AgentActivityState.LISTENING.value
    assert await leases.renew_lease(claimed, now=_real_now()) is not None


async def test_claim_is_refused_after_the_deadline_or_a_termination_request(
    backend: Backend,
) -> None:
    record, _token = await _crashed(backend)
    recovery = MongoWorkerRecoveryRepository(backend.persistence)
    leases = MongoWorkerLeaseRepository(backend.persistence)
    expired_id = new_id()
    await recovery.start_recovery(
        record.session_id,
        owner_instance_id=OWNER,
        recovery_dispatch_id=expired_id,
        now=_real_now() - timedelta(seconds=30),
    )

    late = await leases.claim_recovery(
        record.session_id, REPLACEMENT, recovery_dispatch_id=expired_id, now=_real_now()
    )

    assert late is None
    other, _ = await _crashed(backend)
    other_id = new_id()
    await recovery.start_recovery(
        other.session_id, owner_instance_id=OWNER, recovery_dispatch_id=other_id, now=_real_now()
    )
    sessions = MongoSessionRecordRepository(backend.persistence)
    current = await sessions.get(other.session_id)
    assert current is not None
    ending = current.request_end(
        client_request_id=new_id(),
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=_real_now(),
    )
    await sessions.replace(ending.record, expected_revision=current.state_revision)
    assert (
        await leases.claim_recovery(
            other.session_id, REPLACEMENT, recovery_dispatch_id=other_id, now=_real_now()
        )
        is None
    )


async def test_ownership_renewal_and_takeover_keep_the_recovery_budget(backend: Backend) -> None:
    record, token = await _crashed(backend)
    recovery = MongoWorkerRecoveryRepository(backend.persistence)
    started = await recovery.start_recovery(
        record.session_id,
        owner_instance_id=OWNER,
        recovery_dispatch_id=new_id(),
        now=_real_now() - timedelta(seconds=12),  # ownership expired, deadline 8 s away
    )
    assert started is not None

    renewed = await recovery.renew_ownership(
        record.session_id, owner_instance_id=OWNER, owner_generation=1, now=_real_now()
    )
    taken = await recovery.take_over(
        record.session_id,
        expected_owner_generation=1,
        owner_instance_id="reconciler-other",
        now=_real_now(),
    )
    raw = await _raw(backend, record.session_id)

    assert renewed is None  # an expired ownership lease cannot be renewed
    assert taken is not None
    assert taken.owner_instance_id == "reconciler-other"
    assert taken.owner_generation == 2
    assert taken.recovery_dispatch_id == started.recovery_dispatch_id
    assert taken.recovery_deadline_at == started.recovery_deadline_at
    assert raw["worker_recovery_count"] == 1
    assert raw["worker_assignment"]["writer_epoch"] == token.writer_epoch + 2
    assert (
        await recovery.renew_ownership(
            record.session_id,
            owner_instance_id="reconciler-other",
            owner_generation=2,
            now=_real_now(),
        )
        is not None
    )


async def test_open_turns_of_the_crashed_worker_are_abandoned(backend: Backend) -> None:
    record, _token = await _crashed(backend)
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    open_turn = make_turn(record.session_id, 1)
    await turns.save(open_turn)

    changed = await MongoWorkerRecoveryRepository(backend.persistence).abandon_open_turns(
        record.session_id, now=_real_now()
    )
    stored = await turns.get(open_turn.turn_id)

    assert changed == 1
    assert stored is not None
    assert stored.status is TurnStatus.ABANDONED


async def test_reconciler_recovers_once_then_a_second_crash_fails(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, token = await _crashed(backend)
    transport = RecoveringMockTransport(browser_present=True)
    async with api_factory(
        environ=MONGO_ENV,
        persistence=PersistenceMode.MONGODB,
        mongo=backend.persistence,
        extra_transports={"livekit": transport},
    ) as api:
        api.clock.advance(int((_real_now() - api.clock.utc_now()).total_seconds() * 1000) + 500)
        reconciler = SessionReconciler(api.runtime)
        first = await reconciler.reconcile_session(
            record.session_id, writer_epoch=token.writer_epoch
        )
        renewed = await reconciler.reconcile_session(record.session_id)
        stored = await api.runtime.require_stores().sessions.get(record.session_id)
        assert stored is not None
        assert stored.recovery_authorization is not None
        dispatch_id = stored.recovery_authorization.recovery_dispatch_id
        replacement = await MongoWorkerLeaseRepository(backend.persistence).claim_recovery(
            record.session_id,
            REPLACEMENT,
            recovery_dispatch_id=dispatch_id,
            now=_real_now() - timedelta(seconds=20),  # make the new lease expire at once
        )
        assert replacement is not None
        second = await reconciler.reconcile_session(
            record.session_id, writer_epoch=replacement.writer_epoch
        )
    raw = await _raw(backend, record.session_id)

    assert first is ReconcileAction.CONTINUE_RECOVERY  # started, then continued in one pass
    assert renewed is ReconcileAction.CONTINUE_RECOVERY
    assert list(transport.recovery_dispatches) == [dispatch_id]  # idempotent: one dispatch
    assert transport.recovery_dispatches[dispatch_id].recovery_dispatch_id == dispatch_id
    assert len(transport.recovering_notices) == 1
    assert transport.recovering_notices[0].payload == {"state": "recovering"}
    assert second is ReconcileAction.FAIL_WORKER_LOST  # the recovery budget is spent
    assert raw["status"] == "failed"
    assert raw["worker_recovery_count"] == 1


async def test_exhausted_recovery_converges_to_failed(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, token = await _crashed(backend)
    transport = RecoveringMockTransport(browser_present=True)
    async with api_factory(
        environ=MONGO_ENV,
        persistence=PersistenceMode.MONGODB,
        mongo=backend.persistence,
        extra_transports={"livekit": transport},
    ) as api:
        api.clock.advance(int((_real_now() - api.clock.utc_now()).total_seconds() * 1000) + 500)
        reconciler = SessionReconciler(api.runtime)
        started = await reconciler.reconcile_session(
            record.session_id, writer_epoch=token.writer_epoch
        )
        api.clock.advance(21_000)  # past the 20 s recovery deadline, no replacement claimed
        exhausted = await reconciler.reconcile_session(record.session_id)
        repeated = await reconciler.reconcile_session(record.session_id)
    raw = await _raw(backend, record.session_id)

    assert started is ReconcileAction.CONTINUE_RECOVERY
    assert exhausted is ReconcileAction.FAIL_RECOVERY_EXHAUSTED
    assert repeated is ReconcileAction.NONE
    assert raw["status"] == "failed"
    assert "recovery_authorization" not in raw
    assert "next_reconcile_at" not in raw
    assert transport.released  # room and dispatches cleaned up


async def test_recovery_without_the_browser_fails_at_once(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, token = await _crashed(backend)
    transport = RecoveringMockTransport(browser_present=False)
    async with api_factory(
        environ=MONGO_ENV,
        persistence=PersistenceMode.MONGODB,
        mongo=backend.persistence,
        extra_transports={"livekit": transport},
    ) as api:
        api.clock.advance(int((_real_now() - api.clock.utc_now()).total_seconds() * 1000) + 500)
        await SessionReconciler(api.runtime).reconcile_session(
            record.session_id, writer_epoch=token.writer_epoch
        )
    raw = await _raw(backend, record.session_id)

    assert raw["status"] == "failed"
    assert transport.recovery_dispatches == {}


async def test_another_reconciler_takes_over_an_abandoned_recovery(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, _token = await _crashed(backend)
    started = await MongoWorkerRecoveryRepository(backend.persistence).start_recovery(
        record.session_id,
        owner_instance_id="reconciler-crashed",
        recovery_dispatch_id=new_id(),
        now=_real_now() - timedelta(seconds=12),  # its owner stopped renewing
    )
    assert started is not None
    transport = RecoveringMockTransport(fail_dispatch=True)
    async with api_factory(
        environ=MONGO_ENV,
        persistence=PersistenceMode.MONGODB,
        mongo=backend.persistence,
        extra_transports={"livekit": transport},
    ) as api:
        api.clock.advance(int((_real_now() - api.clock.utc_now()).total_seconds() * 1000) + 500)
        reconciler = SessionReconciler(api.runtime)
        action = await reconciler.reconcile_session(record.session_id)
        stored = await api.runtime.require_stores().sessions.get(record.session_id)

    assert action is ReconcileAction.CONTINUE_RECOVERY
    assert stored is not None
    assert stored.status is SessionStatus.ACTIVE
    assert stored.recovery_authorization is not None
    assert stored.recovery_authorization.owner_instance_id == reconciler.recovery.instance_id
    assert stored.recovery_authorization.owner_generation == 2
    assert stored.recovery_authorization.recovery_dispatch_id == started.recovery_dispatch_id
    assert stored.worker_recovery_count == 1  # a takeover is not a second recovery
    assert transport.recovery_dispatches == {}  # the failed dispatch is retried next pass
    assert record.session_id in reconciler.recovery.owned


async def test_owned_recoveries_are_renewed_on_every_pass(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    record, token = await _crashed(backend)
    transport = RecoveringMockTransport()
    async with api_factory(
        environ=MONGO_ENV,
        persistence=PersistenceMode.MONGODB,
        mongo=backend.persistence,
        extra_transports={"livekit": transport},
    ) as api:
        api.clock.advance(int((_real_now() - api.clock.utc_now()).total_seconds() * 1000) + 500)
        reconciler = SessionReconciler(api.runtime)
        await reconciler.reconcile_session(record.session_id, writer_epoch=token.writer_epoch)
        report = await reconciler.reconcile_once()

    assert ReconcileAction.CONTINUE_RECOVERY in report.actions
