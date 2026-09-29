"""Lease heartbeat, self-fence, and durable end observation (docs/05 §4, §20)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.domain.test_control_session import END_ID, build_record

from voice_agent.agent_worker.lease_keeper import LeaseKeeper
from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.domain.control_session import SessionRecord, TerminationRequester
from voice_agent.domain.worker_lease import LeaseToken, lease_expiry
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.ports.control_plane import StoreUnavailableError

SESSION_ID = "00000000-0000-4000-8000-000000000001"
START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _token(now: datetime) -> LeaseToken:
    return LeaseToken(
        session_id=SESSION_ID,
        generation=1,
        worker_instance_id="w",
        livekit_job_id="j",
        writer_epoch=1,
        lease_revision=1,
        lease_expires_at=lease_expiry(now),
    )


class Leases:
    def __init__(self) -> None:
        self.mode = "ok"
        self.released = 0

    async def renew_lease(self, token: LeaseToken, *, now: datetime) -> LeaseToken | None:
        if self.mode == "fenced":
            return None
        if self.mode == "down":
            raise StoreUnavailableError("down")
        return token.renewed(lease_expires_at=lease_expiry(now))

    async def release(self, token: LeaseToken, *, now: datetime) -> bool:
        self.released += 1
        if self.mode == "down":
            raise StoreUnavailableError("down")
        return True

    async def claim_initial(self, *args: object, **kwargs: object) -> None:
        return None

    async def fence_expired(self, *args: object, **kwargs: object) -> None:
        return None


class Sessions:
    def __init__(self, record: SessionRecord | None) -> None:
        self.record = record

    async def get(self, session_id: str) -> SessionRecord | None:
        return self.record


def _keeper(clock: ManualClock, leases: Leases, sessions: Sessions) -> LeaseKeeper:
    async def sleep(seconds: float) -> None:
        clock.advance(int(seconds * 1000))

    return LeaseKeeper(
        leases=leases,  # type: ignore[arg-type]
        sessions=sessions,
        token=_token(clock.utc_now()),
        clock=clock,
        sleep=sleep,
    )


def _record(**update: object) -> SessionRecord:
    return build_record().model_copy(update=update)


@pytest.mark.asyncio
async def test_successful_beats_renew_and_extend_the_local_deadline() -> None:
    clock = ManualClock(START)
    keeper = _keeper(clock, Leases(), Sessions(_record(status=SessionStatus.ACTIVE)))

    for _ in range(3):
        clock.advance(5000)
        await keeper.beat()

    assert keeper.renewals == 3
    assert keeper.token.lease_revision == 4
    assert not keeper.fenced.is_set()
    assert 0 < keeper.lease_valid_for_ms() <= 12_000


@pytest.mark.asyncio
async def test_rejected_renewal_self_fences_immediately() -> None:
    clock = ManualClock(START)
    leases = Leases()
    leases.mode = "fenced"
    keeper = _keeper(clock, leases, Sessions(_record()))

    await keeper.run()

    assert keeper.fenced.is_set()


@pytest.mark.asyncio
async def test_store_outage_self_fences_only_after_the_local_deadline() -> None:
    clock = ManualClock(START)
    leases = Leases()
    leases.mode = "down"
    keeper = _keeper(clock, leases, Sessions(_record()))

    clock.advance(5000)
    await keeper.beat()
    early = keeper.fenced.is_set()
    clock.advance(7000)
    await keeper.beat()

    assert not early
    assert keeper.fenced.is_set()  # 15 s lease - 3 s safety margin
    assert keeper.lease_valid_for_ms() == 0


@pytest.mark.asyncio
async def test_durable_termination_request_is_observed() -> None:
    clock = ManualClock(START)
    ending = (
        _record()
        .request_end(
            client_request_id=END_ID,
            reason=DisconnectReason.USER_ENDED,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=START,
        )
        .record
    )
    keeper = _keeper(clock, Leases(), Sessions(ending))

    await keeper.beat()

    assert keeper.end_requested.is_set()
    assert keeper.termination_revision == 1


@pytest.mark.asyncio
async def test_terminal_or_missing_session_fences_the_worker() -> None:
    clock = ManualClock(START)
    terminal = _keeper(clock, Leases(), Sessions(_record(status=SessionStatus.FAILED)))
    missing = _keeper(clock, Leases(), Sessions(None))

    await terminal.beat()
    await missing.beat()

    assert terminal.fenced.is_set()
    assert missing.fenced.is_set()


@pytest.mark.asyncio
async def test_release_is_best_effort() -> None:
    clock = ManualClock(START)
    leases = Leases()
    keeper = _keeper(clock, leases, Sessions(_record()))

    assert await keeper.release()
    leases.mode = "down"
    assert not await keeper.release()


def test_local_deadline_uses_safety_margin() -> None:
    token = _token(START)

    assert token.local_deadline(START) == START + timedelta(seconds=12)
