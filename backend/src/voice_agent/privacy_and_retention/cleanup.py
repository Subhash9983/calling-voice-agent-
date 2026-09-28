"""Bounded scheduled R&D session cleanup job (docs/02 §20; docs/03 §21).

Runs at most :data:`MAX_CLEANUP_BATCH_SESSIONS` expired sessions per batch,
child-first through :class:`RetentionCleanupStore`, isolating failures per
session (a failed child deletion preserves its parent and is retried by the
next idempotent run). Dry-run is the default; a real run is explicit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from voice_agent.ports.clock import Clock
from voice_agent.ports.persistence import RetentionCleanupStore, SessionCleanupReport
from voice_agent.privacy_and_retention.expiry import MAX_CLEANUP_BATCH_SESSIONS


@dataclass(frozen=True, slots=True)
class CleanupRunReport:
    environment: str
    dry_run: bool
    sessions: tuple[SessionCleanupReport, ...]

    @property
    def deleted_sessions(self) -> int:
        return sum(1 for item in self.sessions if item.parent_deleted)

    @property
    def failures(self) -> tuple[SessionCleanupReport, ...]:
        return tuple(item for item in self.sessions if item.failure is not None)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "dry_run": self.dry_run,
            "selected_sessions": len(self.sessions),
            "deleted_sessions": self.deleted_sessions,
            "failures": [
                {"session_id": item.session_id, "reason": item.failure} for item in self.failures
            ],
            "candidates": _totals(item.candidates for item in self.sessions),
            "deleted": _totals(item.deleted for item in self.sessions),
        }


def _totals(rows: Iterable[Mapping[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        for key, value in row.items():
            totals[key] = totals.get(key, 0) + value
    return totals


class RetentionCleanupJob:
    def __init__(self, store: RetentionCleanupStore, *, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def run(
        self,
        environment: str,
        *,
        dry_run: bool = True,
        limit: int = MAX_CLEANUP_BATCH_SESSIONS,
    ) -> CleanupRunReport:
        now = self._clock.utc_now()
        batch = max(1, min(limit, MAX_CLEANUP_BATCH_SESSIONS))
        selected = await self._store.select_expired_sessions(environment, now=now, limit=batch)
        reports = []
        for session_id in selected:
            reports.append(await self._store.cleanup_session(session_id, now=now, dry_run=dry_run))
        return CleanupRunReport(environment=environment, dry_run=dry_run, sessions=tuple(reports))
