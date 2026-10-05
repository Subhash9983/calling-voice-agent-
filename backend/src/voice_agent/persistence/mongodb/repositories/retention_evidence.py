"""Read-only retention evidence over the approved expiry indexes (docs/02 §20; WP11).

Session-level counts use ``ix_sessions_expiry``/status filters scoped to one
environment. Child scheduling is checked for a bounded sample of the most
recently ended terminal sessions, each child count scoped by ``session_id``
and, for events and errors, to the classes ordinary cleanup owns. Nothing is
written or deleted here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import TERMINAL_STATES
from voice_agent.persistence.mongodb.repositories.base import MongoRepository, bounded_limit
from voice_agent.persistence.mongodb.repositories.retention import CLEANUP_ORDER
from voice_agent.privacy_and_retention.expiry import (
    MAX_CLEANUP_BATCH_SESSIONS,
    RD_RETENTION_DAYS,
    RETENTION_POLICY_VERSION,
)
from voice_agent.privacy_and_retention.retention_evidence import RetentionOverview

DEFAULT_SAMPLE: Final = 20


class MongoRetentionEvidenceStore(MongoRepository):
    async def overview(
        self, environment: str, *, now: datetime, sample: int = DEFAULT_SAMPLE
    ) -> RetentionOverview:
        terminal: dict[str, Any] = {
            "environment": environment,
            "status": {"$in": list(TERMINAL_STATES)},
        }
        scheduled = {**terminal, "expires_at": {"$exists": True}}
        sessions = self.collection(Collection.VOICE_SESSIONS)
        async with translate_errors():
            terminal_count = await sessions.count_documents(terminal)
            scheduled_count = await sessions.count_documents(scheduled)
            due = await sessions.count_documents({**terminal, "expires_at": {"$lte": to_bson(now)}})
            upcoming = await sessions.find_one(
                {**terminal, "expires_at": {"$gt": to_bson(now)}},
                projection={"expires_at": 1, "_id": 0},
                sort=[("expires_at", 1)],
            )
        gaps, with_gaps, sampled = await self._child_gaps(terminal, sample)
        return RetentionOverview(
            environment=environment,
            policy_version=RETENTION_POLICY_VERSION,
            retention_days=RD_RETENTION_DAYS,
            checked_at=now,
            terminal_sessions=terminal_count,
            scheduled_sessions=scheduled_count,
            unscheduled_terminal_sessions=terminal_count - scheduled_count,
            due_sessions=due,
            next_due_at=None if upcoming is None else upcoming.get("expires_at"),
            sampled_sessions=sampled,
            unscheduled_children=gaps,
            sessions_with_gaps=tuple(with_gaps),
        )

    async def _child_gaps(
        self, terminal: dict[str, Any], sample: int
    ) -> tuple[dict[str, int], list[str], int]:
        batch = bounded_limit(max(sample, 1), MAX_CLEANUP_BATCH_SESSIONS)
        async with translate_errors():
            rows = await (
                self.collection(Collection.VOICE_SESSIONS)
                .find(
                    terminal,
                    projection={"session_id": 1, "_id": 0},
                    sort=[("ended_at", -1)],
                    limit=batch,
                )
                .to_list(length=batch)
            )
        gaps: dict[str, int] = {collection.value: 0 for collection, _ in CLEANUP_ORDER}
        with_gaps: list[str] = []
        for row in rows:
            session_id = str(row["session_id"])
            missing = await self._unscheduled(session_id)
            for name, count in missing.items():
                gaps[name] += count
            if any(missing.values()):
                with_gaps.append(session_id)
        return gaps, with_gaps, len(rows)

    async def _unscheduled(self, session_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with translate_errors():
            for collection, scope in CLEANUP_ORDER:
                filters = {"session_id": session_id, "expires_at": {"$exists": False}, **scope}
                counts[collection.value] = await self.collection(collection).count_documents(
                    filters
                )
        return counts
