"""Worker lease heartbeat, self-fencing, and durable end observation (docs/05 §4, §20).

Every 5 s the keeper renews the lease with the fenced compare-and-set, keeps
a conservative local deadline (the renewed expiry measured from when the
renewal was sent, minus the 3 s safety margin), and reloads the durable
session. It signals:

- ``fenced`` when a renewal is rejected (writer epoch/generation mismatch or
  expired lease), the local deadline passes without a successful renewal, or
  another writer already terminalized the session;
- ``end_requested`` when the durable ``termination_request`` is observed
  (the recovery path when the ``va.control.v1`` packet is lost).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import Final

from voice_agent.agent_worker.admission import SessionReader
from voice_agent.domain.worker_lease import HEARTBEAT_INTERVAL_MS, LeaseToken
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError
from voice_agent.ports.session_lifecycle import WorkerLeaseRepository

HEARTBEAT_INTERVAL_S: Final = HEARTBEAT_INTERVAL_MS / 1000
STORE_CALL_TIMEOUT_S: Final = 2.0
_TRANSIENT: Final = (StoreUnavailableError, PersistenceRejectedError, TimeoutError)
Sleep = Callable[[float], Awaitable[None]]


class LeaseKeeper:
    def __init__(
        self,
        *,
        leases: WorkerLeaseRepository,
        sessions: SessionReader,
        token: LeaseToken,
        clock: Clock,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._leases = leases
        self._sessions = sessions
        self._token = token
        self._clock = clock
        self._interval_s = interval_s
        self._sleep = sleep
        self._deadline: datetime = token.local_deadline(clock.utc_now())
        self.fenced = asyncio.Event()
        self.end_requested = asyncio.Event()
        self._stopping = asyncio.Event()
        self.termination_revision: int | None = None
        self.renewals = 0

    @property
    def token(self) -> LeaseToken:
        return self._token

    def _fence(self) -> None:
        self.fenced.set()

    async def run(self) -> None:
        """Heartbeat until fenced or stopped; an in-flight renewal always completes."""
        while not self.fenced.is_set() and not self._stopping.is_set():
            await self._pause()
            if self._stopping.is_set():
                return
            await self.beat()

    async def _pause(self) -> None:
        sleeper = asyncio.ensure_future(self._sleep(self._interval_s))
        stopper = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            stopper.cancel()

    def stop(self) -> None:
        """Ask the heartbeat loop to finish after any in-flight store call."""
        self._stopping.set()

    async def beat(self) -> None:
        sent_at = self._clock.utc_now()
        renewed: LeaseToken | None = None
        transient = False
        try:
            async with asyncio.timeout(STORE_CALL_TIMEOUT_S):
                renewed = await self._leases.renew_lease(self._token, now=sent_at)
        except _TRANSIENT:
            transient = True
        if renewed is None:
            if not transient or self._clock.utc_now() >= self._deadline:
                self._fence()
            return
        self._token, self.renewals = renewed, self.renewals + 1
        self._deadline = renewed.local_deadline(sent_at)
        await self._observe()

    async def _observe(self) -> None:
        with suppress(*_TRANSIENT):
            async with asyncio.timeout(STORE_CALL_TIMEOUT_S):
                record = await self._sessions.get(self._token.session_id)
            if record is None or record.is_terminal:
                self._fence()
            elif record.termination_request is not None:
                self.termination_revision = record.termination_request.revision
                self.end_requested.set()

    def lease_valid_for_ms(self) -> int:
        """Remaining local lease time (the browser lease-timing hint, docs/06 §14)."""
        remaining = self._deadline - self._clock.utc_now()
        return max(0, int(remaining.total_seconds() * 1000))

    async def release(self) -> bool:
        """Clean release by the owning assignment; ``False`` when already fenced."""
        try:
            async with asyncio.timeout(STORE_CALL_TIMEOUT_S):
                return await self._leases.release(self._token, now=self._clock.utc_now())
        except _TRANSIENT:
            return False
