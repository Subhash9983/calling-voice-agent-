"""Bounded control-API session reconciler (docs/05 §21; docs/04 §9; docs/06 §6, §16).

One background task per control-API process scans indexed nonterminal
sessions whose ``next_reconcile_at`` or worker lease is due every 5 s, plus
sessions nudged by an accepted end request without a live worker. For each it
applies :func:`reconcile_action` under ``state_revision`` compare-and-set
(re-read and retry at most 3 times):

- a maximum-duration session gets a ``system_timeout`` termination request
  (and the worker, if live, the ``va.control.v1`` wake-up);
- a connection/start timeout before ``active`` fails as ``transport_error``;
- an ``ending`` session without a live worker completes as ``ended`` or
  controlled ``failed`` from its recorded reason (missing end packet or no
  worker still reaches a terminal state);
- an ``active`` session whose worker lease expired is fenced (``writer_epoch``
  increment) and failed: worker-crash recovery is not part of this build.

A terminal session's room/dispatch is then deleted best-effort; a cleanup
failure is logged with a safe code and never reopens the session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.control_api.runtime import ControlPlaneRuntime, ControlPlaneStores
from voice_agent.control_api.services.common import (
    MAX_CAS_ATTEMPTS,
    allocation_of,
    emit_session_event,
    notify_worker_end,
    release_transport,
    try_replace,
)
from voice_agent.control_api.structured_logging import get_logger, log_event
from voice_agent.domain.control_session import SessionRecord, TerminationRequester
from voice_agent.domain.session_reconcile import ReconcileAction, reconcile_action
from voice_agent.domain.worker_lease import RECONCILE_INTERVAL_MS
from voice_agent.ports.session_lifecycle import MAX_RECONCILE_BATCH, ReconcileCandidate

RECONCILE_INTERVAL_S: Final = RECONCILE_INTERVAL_MS / 1000


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    scanned: int
    actions: tuple[ReconcileAction, ...]


def _terminal_event(record: SessionRecord) -> EventType:
    return (
        EventType.SESSION_ENDED
        if record.status is SessionStatus.ENDED
        else EventType.SESSION_FAILED
    )


class SessionReconciler:
    def __init__(self, runtime: ControlPlaneRuntime) -> None:
        self._runtime = runtime
        self._nudges = runtime.nudges

    def _stores(self) -> ControlPlaneStores:
        return self._runtime.require_stores()

    async def run(self, stop: asyncio.Event, *, interval_s: float = RECONCILE_INTERVAL_S) -> None:
        """Scan every ``interval_s`` until ``stop``; a failed pass is logged and retried."""
        while not stop.is_set():
            await self._wait(stop, interval_s)
            if stop.is_set():
                return
            try:
                await self.reconcile_once()
            except Exception:  # a background pass must never kill the loop
                log_event(get_logger(), logging.WARNING, "reconciler.pass_failed")

    async def _wait(self, stop: asyncio.Event, interval_s: float) -> None:
        """Sleep until the interval passes, a nudge arrives, or ``stop`` is set."""
        waiters = [
            asyncio.ensure_future(stop.wait()),
            asyncio.ensure_future(self._nudges.wakeup.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=interval_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _candidates(self) -> Sequence[ReconcileCandidate]:
        stores, runtime = self._stores(), self._runtime
        scans = stores.reconciliation
        if scans is None:
            return ()
        environment = runtime.require_settings().app_env.value
        now = runtime.clock.utc_now()
        due = await runtime.bounded(
            scans.list_reconcile_due(environment, now=now, limit=MAX_RECONCILE_BATCH)
        )
        leases = await runtime.bounded(
            scans.list_lease_due(environment, now=now, limit=MAX_RECONCILE_BATCH)
        )
        unique = {candidate.session_id: candidate for candidate in (*due, *leases)}
        return tuple(unique.values())

    async def reconcile_once(self) -> ReconcileReport:
        candidates = {c.session_id: c.writer_epoch for c in await self._candidates()}
        for session_id in self._nudges.drain():
            candidates.setdefault(session_id, None)
        actions = []
        for session_id, writer_epoch in candidates.items():
            actions.append(await self.reconcile_session(session_id, writer_epoch=writer_epoch))
        return ReconcileReport(scanned=len(candidates), actions=tuple(actions))

    async def reconcile_session(
        self, session_id: str, *, writer_epoch: int | None = None
    ) -> ReconcileAction:
        """Apply the due action; returns the last action taken (``NONE`` when idle)."""
        taken = ReconcileAction.NONE
        for _attempt in range(MAX_CAS_ATTEMPTS + 1):
            record = await self._runtime.bounded(self._stores().sessions.get(session_id))
            if record is None:
                return taken
            action = reconcile_action(record, self._runtime.clock.utc_now())
            if action is ReconcileAction.NONE:
                return taken
            if await self._apply(record, action, writer_epoch):
                taken = action
                if action not in _CONTINUES:
                    return taken
            # A revision conflict or a continuing action re-reads and re-evaluates.
        return taken

    async def _apply(
        self, record: SessionRecord, action: ReconcileAction, writer_epoch: int | None
    ) -> bool:
        if action is ReconcileAction.REQUEST_MAXIMUM_DURATION_END:
            return await self._request_maximum_duration_end(record)
        await self._fence(record, writer_epoch)
        now = self._runtime.clock.utc_now()
        if action is ReconcileAction.FINALIZE_END:
            final = record.finalize_end(now=now)
        else:
            final = record.fail(DisconnectReason.TRANSPORT_ERROR, now=now)
        if not await try_replace(self._runtime, final, expected_revision=record.state_revision):
            return False
        await self._terminal_evidence(final, action)
        return True

    async def _request_maximum_duration_end(self, record: SessionRecord) -> bool:
        runtime = self._runtime
        decision = record.request_end(
            client_request_id=runtime.ids.new_id(),
            reason=DisconnectReason.MAXIMUM_DURATION,
            requested_by=TerminationRequester.SYSTEM_TIMEOUT,
            now=runtime.clock.utc_now(),
        )
        if not decision.accepted:
            return False
        if not await try_replace(runtime, decision.record, expected_revision=record.state_revision):
            return False
        await emit_session_event(
            runtime,
            decision.record,
            EventType.SESSION_END_REQUESTED,
            payload={"reason": DisconnectReason.MAXIMUM_DURATION.value},
        )
        await notify_worker_end(runtime, decision.record)
        return True

    async def _fence(self, record: SessionRecord, writer_epoch: int | None) -> None:
        """Fence an expired, unreleased worker lease so the old worker cannot write."""
        leases = self._stores().leases
        if leases is None or writer_epoch is None or record.worker_lease_expires_at is None:
            return
        now = self._runtime.clock.utc_now()
        await self._runtime.bounded(
            leases.fence_expired(record.session_id, expected_writer_epoch=writer_epoch, now=now)
        )

    async def _terminal_evidence(self, final: SessionRecord, action: ReconcileAction) -> None:
        runtime = self._runtime
        if action is ReconcileAction.FAIL_WORKER_LOST:
            await emit_session_event(runtime, final, EventType.WORKER_LEASE_EXPIRED)
        reason = final.disconnect_reason.value if final.disconnect_reason else "unknown"
        event_type = _terminal_event(final)
        await emit_session_event(
            runtime,
            final,
            event_type,
            severity=EventSeverity.INFO
            if event_type is EventType.SESSION_ENDED
            else EventSeverity.ERROR,
            payload={"disconnect_reason": reason},
        )
        binding = final.transport
        transport = None if binding is None else runtime.transports.get(binding.provider)
        if binding is not None and transport is not None and transport.is_available:
            await release_transport(runtime, transport, allocation_of(binding))


# Actions after which the same session may need another step in this pass
# (a maximum-duration end without a live worker can be finalized at once).
_CONTINUES: Final = frozenset({ReconcileAction.REQUEST_MAXIMUM_DURATION_END})
