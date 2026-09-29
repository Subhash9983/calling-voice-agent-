"""Session reconciler backstop over the in-memory stores (docs/05 §21; docs/04 §9).

Each test drives :meth:`SessionReconciler.reconcile_once` explicitly with the
harness's manual clock; no background task runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

import pytest
from tests.integration.control_api.conftest import API, Api, ApiFactory, new_id

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.contracts.events import EventType
from voice_agent.control_api.reconciler import SessionReconciler
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.session_reconcile import ReconcileAction
from voice_agent.persistence.control_plane_memory import InMemorySessionReconciliation
from voice_agent.ports.control_plane import EventQuery

pytestmark = pytest.mark.asyncio


def _reconciler(api: Api) -> SessionReconciler:
    stores = api.runtime.require_stores()
    runtime: ControlPlaneRuntime = dataclasses.replace(
        api.runtime,
        stores=dataclasses.replace(
            stores, reconciliation=InMemorySessionReconciliation(api.sessions)
        ),
    )
    return SessionReconciler(runtime)


async def _record(api: Api, session_id: str) -> SessionRecord:
    record = await api.sessions.get(session_id)
    assert record is not None
    return record


async def _set(api: Api, record: SessionRecord, **update: object) -> SessionRecord:
    changed = record.model_copy(update=update)
    api.sessions._items[record.session_id] = changed  # test seam: store-owned fields
    return changed


async def _event_types(api: Api, session_id: str) -> list[str]:
    events = await api.timeline.list_events(
        EventQuery(session_id=session_id, limit=100, browser_safe_only=False)
    )
    return [item.envelope.event_type.value for item in events]


async def _end(api: Api, session_id: str) -> None:
    response = await api.client.post(
        f"{API}/sessions/{session_id}/end",
        json={"client_request_id": new_id(), "reason": "user_ended"},
    )
    assert response.status_code == 202


async def test_connect_timeout_without_worker_fails_and_cleans_up(api: Api) -> None:
    session_id = await api.new_session_id()
    reconciler = _reconciler(api)

    early = await reconciler.reconcile_once()
    api.clock.advance(20_001)
    late = await reconciler.reconcile_once()
    record = await _record(api, session_id)

    assert early.actions == ()
    assert late.actions == (ReconcileAction.FAIL_CONNECT_TIMEOUT,)
    assert record.status is SessionStatus.FAILED
    assert record.disconnect_reason is DisconnectReason.TRANSPORT_ERROR
    assert record.transport is not None
    assert api.transport.released == [record.transport.external_room_id]
    assert (await _event_types(api, session_id))[-1] == "session.failed"


async def test_missing_end_packet_and_no_worker_still_terminalizes(api: Api) -> None:
    session_id = await api.new_session_id()
    await _end(api, session_id)

    report = await _reconciler(api).reconcile_once()
    record = await _record(api, session_id)

    assert report.actions == (ReconcileAction.FINALIZE_END,)
    assert record.status is SessionStatus.ENDED
    assert record.disconnect_reason is DisconnectReason.USER_ENDED
    assert api.transport.signals == []  # no live worker: no wake-up packet was sent
    assert (await _event_types(api, session_id))[-2:] == [
        "session.end_requested",
        "session.ended",
    ]


async def test_end_with_live_worker_sends_targeted_signal_and_waits(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await _record(api, session_id)
    lease = api.clock.utc_now() + timedelta(seconds=15)
    await _set(api, record, worker_lease_expires_at=lease)
    await _end(api, session_id)
    reconciler = _reconciler(api)

    waiting = await reconciler.reconcile_session(session_id)
    api.clock.advance(15_000)
    finalized = await reconciler.reconcile_session(session_id)

    [signal] = api.transport.signals
    assert signal.session_id == session_id
    assert signal.payload.termination_request_revision == 1
    assert waiting is ReconcileAction.NONE
    assert finalized is ReconcileAction.FINALIZE_END
    assert (await _record(api, session_id)).status is SessionStatus.ENDED


async def test_maximum_duration_without_worker_ends_in_one_pass(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await _record(api, session_id)
    await _set(api, record, connect_deadline_at=None)
    api.clock.advance(record.maximum_session_ms)

    action = await _reconciler(api).reconcile_session(session_id)
    final = await _record(api, session_id)

    assert action is ReconcileAction.FINALIZE_END
    assert final.status is SessionStatus.ENDED
    assert final.disconnect_reason is DisconnectReason.MAXIMUM_DURATION
    assert final.termination_request is not None
    assert final.termination_request.requested_by.value == "system_timeout"


async def test_maximum_duration_with_live_worker_only_requests_end(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await _record(api, session_id)
    api.clock.advance(record.maximum_session_ms)
    await _set(api, record, worker_lease_expires_at=api.clock.utc_now() + timedelta(seconds=10))

    action = await _reconciler(api).reconcile_session(session_id)
    final = await _record(api, session_id)

    assert action is ReconcileAction.REQUEST_MAXIMUM_DURATION_END
    assert final.status is SessionStatus.ENDING
    assert len(api.transport.signals) == 1


async def test_active_session_whose_worker_vanished_fails(api: Api) -> None:
    session_id = await api.new_session_id()
    record = await _record(api, session_id)
    lease = api.clock.utc_now() + timedelta(seconds=15)
    await _set(
        api,
        record,
        status=SessionStatus.ACTIVE,
        state_revision=record.state_revision + 1,
        connect_deadline_at=None,
        worker_lease_expires_at=lease,
    )
    api.clock.advance(15_001)

    report = await _reconciler(api).reconcile_once()
    final = await _record(api, session_id)

    assert report.actions == (ReconcileAction.FAIL_WORKER_LOST,)
    assert final.status is SessionStatus.FAILED
    types = await _event_types(api, session_id)
    assert types[-2:] == [EventType.WORKER_LEASE_EXPIRED.value, EventType.SESSION_FAILED.value]


async def test_cleanup_failure_never_reopens_the_session(api: Api) -> None:
    session_id = await api.new_session_id()
    api.transport.fail_release = True
    await _end(api, session_id)

    await _reconciler(api).reconcile_once()

    assert (await _record(api, session_id)).status is SessionStatus.ENDED


async def test_repeated_passes_are_idempotent(api: Api) -> None:
    session_id = await api.new_session_id()
    await _end(api, session_id)
    reconciler = _reconciler(api)

    await reconciler.reconcile_once()
    second = await reconciler.reconcile_once()
    types = await _event_types(api, session_id)

    assert second.actions == ()
    assert types.count("session.ended") == 1


async def test_background_task_is_nudged_by_an_end_without_worker(
    api_factory: ApiFactory,
) -> None:
    async with api_factory(reconcile=True) as api:
        session_id = await api.new_session_id()
        await _end(api, session_id)
        for _attempt in range(100):
            if (await _record(api, session_id)).status is SessionStatus.ENDED:
                break
            await asyncio.sleep(0.02)

        assert (await _record(api, session_id)).status is SessionStatus.ENDED


async def test_unknown_session_nudge_is_harmless(api: Api) -> None:
    reconciler = _reconciler(api)
    api.runtime.nudges.nudge(new_id())

    report = await reconciler.reconcile_once()

    assert report.actions == (ReconcileAction.NONE,)


class _BrokenScans:
    def __init__(self) -> None:
        self.calls = 0

    async def list_reconcile_due(self, *_args: object, **_kwargs: object) -> list[object]:
        self.calls += 1
        raise RuntimeError("store detail")

    async def list_lease_due(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_run_loop_survives_failed_passes_and_stops(api: Api) -> None:
    scans = _BrokenScans()
    stores = api.runtime.require_stores()
    runtime = dataclasses.replace(
        api.runtime, stores=dataclasses.replace(stores, reconciliation=scans)
    )
    stop = asyncio.Event()

    task = asyncio.create_task(SessionReconciler(runtime).run(stop, interval_s=0.01))
    await asyncio.sleep(0.08)
    stop.set()
    await asyncio.wait_for(task, 1)

    assert scans.calls >= 2


@pytest.mark.asyncio
async def test_reconciler_without_scans_only_serves_nudges(api: Api) -> None:
    session_id = await api.new_session_id()
    await _end(api, session_id)
    api.runtime.nudges.nudge(session_id)

    report = await SessionReconciler(api.runtime).reconcile_once()

    assert report.actions == (ReconcileAction.FINALIZE_END,)
