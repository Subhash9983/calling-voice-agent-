"""Worker lease, reconciliation, and retention ports for ``voice_sessions`` (docs/02 §6, §20).

The lease path is independent of business state: an initial claim is a
``state_revision``-checked update, but a heartbeat renewal matches the
assignment tuple, ``writer_epoch`` and ``lease_revision`` and requires an
unexpired lease; it never changes ``state_revision``, ``updated_at`` or
``last_activity_at``. Reconciliation scans read only the approved partial
indexes and are bounded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.worker_lease import LeaseToken, WorkerClaim

MAX_RECONCILE_BATCH = 100


@dataclass(frozen=True, slots=True)
class ReconcileCandidate:
    session_id: str
    status: SessionStatus
    state_revision: int
    next_reconcile_at: datetime | None
    lease_expires_at: datetime | None
    writer_epoch: int | None


@runtime_checkable
class WorkerLeaseRepository(Protocol):
    async def claim_initial(
        self, session_id: str, claim: WorkerClaim, *, expected_revision: int, now: datetime
    ) -> LeaseToken | None:
        """Generation-1 claim of a ``connecting`` session with no live assignment.

        Returns ``None`` when the session changed, already has an unexpired
        assignment, has a termination request, or is under recovery.
        """
        ...

    async def renew_lease(self, token: LeaseToken, *, now: datetime) -> LeaseToken | None:
        """Heartbeat compare-and-set; ``None`` when fenced, stale, or expired."""
        ...

    async def release(self, token: LeaseToken, *, now: datetime) -> bool:
        """Clean release by the owning assignment; removes the lease expiry."""
        ...

    async def fence_expired(
        self, session_id: str, *, expected_writer_epoch: int, now: datetime
    ) -> int | None:
        """Reconciler fence of an expired lease; returns the new ``writer_epoch``."""
        ...


@runtime_checkable
class SessionReconciliationRepository(Protocol):
    async def list_reconcile_due(
        self, environment: str, *, now: datetime, limit: int
    ) -> Sequence[ReconcileCandidate]:
        """Nonterminal sessions due for reconciliation (``ix_sessions_reconcile_due``)."""
        ...

    async def list_lease_due(
        self, environment: str, *, now: datetime, limit: int
    ) -> Sequence[ReconcileCandidate]:
        """Nonterminal sessions with an expired unreleased lease (``ix_sessions_lease_due``)."""
        ...


@runtime_checkable
class SessionRetentionRepository(Protocol):
    async def propagate_session_expiry(self, session_id: str) -> int:
        """Copy the terminal retention anchor to every child record; returns updates."""
        ...
