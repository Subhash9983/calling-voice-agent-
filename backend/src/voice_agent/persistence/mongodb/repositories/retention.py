"""Retention-anchor propagation and bounded child-first session cleanup (docs/02 §20).

Propagation copies ``expires_at = ended_at + 30 days`` to every child of a
terminal session; ``consent``/``billing`` events get the later consent-aware
expiry (or stay unset while a consent expiry is unknown). Cleanup deletes
children in the approved order, verifies each child count reaches zero, and
only then deletes the parent. Consent records, protected events, and
security/billing errors are never removed here. Every filter is scoped by
``session_id``; nothing targets a whole collection or database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import TERMINAL_STATES
from voice_agent.persistence.mongodb.repositories.base import MongoRepository, bounded_limit
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError, SessionCleanupReport
from voice_agent.privacy_and_retention.expiry import (
    MAX_CLEANUP_BATCH_SESSIONS,
    PROTECTED_EVENT_CLASSES,
    SESSION_CLEANUP_EVENT_CLASSES,
    protected_event_expires_at,
    session_expires_at,
)

_ORDINARY_EVENTS: Final = sorted(c.value for c in SESSION_CLEANUP_EVENT_CLASSES)
_PROTECTED_EVENTS: Final = sorted(c.value for c in PROTECTED_EVENT_CLASSES)
_PLAIN_CHILDREN: Final = (
    Collection.CONVERSATION_TURNS,
    Collection.PROVIDER_OPERATIONS,
    Collection.ERROR_EVENTS,
    Collection.USER_FEEDBACK,
    Collection.COST_ENTRIES,
)
# Approved deletion order (docs/02 §20) with each child's class scope.
CLEANUP_ORDER: Final[tuple[tuple[Collection, dict[str, Any]], ...]] = (
    (Collection.USER_FEEDBACK, {}),
    (Collection.ERROR_EVENTS, {"retention_class": "operational_error"}),
    (Collection.COST_ENTRIES, {}),
    (Collection.PROVIDER_OPERATIONS, {}),
    (Collection.SESSION_EVENTS, {"retention_class": {"$in": _ORDINARY_EVENTS}}),
    (Collection.CONVERSATION_TURNS, {}),
)


class SessionExpiryPropagator(MongoRepository):
    async def propagate_session_expiry(self, session_id: str) -> int:
        session = await self._terminal_session(session_id)
        if session is None:
            return 0
        ended_at: datetime = session["ended_at"]
        expires = to_bson(session_expires_at(ended_at))
        missing = {"session_id": session_id, "expires_at": {"$exists": False}}
        updated = 0
        async with translate_errors():
            for collection in _PLAIN_CHILDREN:
                result = await self.collection(collection).update_many(
                    missing, {"$set": {"expires_at": expires}}
                )
                updated += result.modified_count
            ordinary = {**missing, "retention_class": {"$in": _ORDINARY_EVENTS}}
            result = await self.collection(Collection.SESSION_EVENTS).update_many(
                ordinary, {"$set": {"expires_at": expires}}
            )
            updated += result.modified_count
        return updated + await self._protected_events(session_id, ended_at)

    async def _terminal_session(self, session_id: str) -> dict[str, Any] | None:
        async with translate_errors():
            raw = await self.collection(Collection.VOICE_SESSIONS).find_one(
                {"session_id": session_id, "status": {"$in": list(TERMINAL_STATES)}},
                projection={"ended_at": 1, "_id": 0},
            )
        if raw is None or raw.get("ended_at") is None:
            return None
        return raw

    async def _protected_events(self, session_id: str, ended_at: datetime) -> int:
        async with translate_errors():
            consents = await (
                self.collection(Collection.CONSENT_RECORDS)
                .find({"session_id": session_id}, projection={"expires_at": 1, "_id": 0})
                .to_list(length=MAX_CLEANUP_BATCH_SESSIONS)
            )
        expiry = protected_event_expires_at(ended_at, [row.get("expires_at") for row in consents])
        if expiry is None:
            return 0
        filters = {
            "session_id": session_id,
            "expires_at": {"$exists": False},
            "retention_class": {"$in": _PROTECTED_EVENTS},
        }
        async with translate_errors():
            result = await self.collection(Collection.SESSION_EVENTS).update_many(
                filters, {"$set": {"expires_at": to_bson(expiry)}}
            )
        return result.modified_count


class MongoRetentionCleanupStore(MongoRepository):
    async def select_expired_sessions(
        self, environment: str, *, now: datetime, limit: int
    ) -> list[str]:
        batch = bounded_limit(limit, MAX_CLEANUP_BATCH_SESSIONS)
        async with translate_errors():
            rows = await (
                self.collection(Collection.VOICE_SESSIONS)
                .find(
                    self._eligible(environment=environment, now=now),
                    projection={"session_id": 1, "_id": 0},
                    sort=[("expires_at", 1)],
                    limit=batch,
                )
                .to_list(length=batch)
            )
        return [str(row["session_id"]) for row in rows]

    async def cleanup_session(
        self, session_id: str, *, now: datetime, dry_run: bool
    ) -> SessionCleanupReport:
        try:
            return await self._cleanup(session_id, now=now, dry_run=dry_run)
        except StoreUnavailableError:
            return SessionCleanupReport(session_id, candidates={}, failure="store_unavailable")
        except PersistenceRejectedError:
            return SessionCleanupReport(session_id, candidates={}, failure="store_rejected")

    async def _cleanup(
        self, session_id: str, *, now: datetime, dry_run: bool
    ) -> SessionCleanupReport:
        eligible = {**self._eligible(now=now), "session_id": session_id}
        async with translate_errors():
            parent = await self.collection(Collection.VOICE_SESSIONS).count_documents(eligible)
        if parent == 0:
            return SessionCleanupReport(session_id, candidates={}, failure="not_eligible")
        candidates = await self._child_counts(session_id)
        if dry_run:
            return SessionCleanupReport(session_id, candidates=candidates)
        deleted: dict[str, int] = {}
        for collection, scope in CLEANUP_ORDER:
            filters = {"session_id": session_id, **scope}
            async with translate_errors():
                result = await self.collection(collection).delete_many(filters)
                remaining = await self.collection(collection).count_documents(filters)
            deleted[collection.value] = result.deleted_count
            if remaining != 0:
                return SessionCleanupReport(
                    session_id, candidates=candidates, deleted=deleted, failure="child_remaining"
                )
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).delete_one(eligible)
        return SessionCleanupReport(
            session_id,
            candidates=candidates,
            deleted=deleted,
            parent_deleted=result.deleted_count == 1,
        )

    async def _child_counts(self, session_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with translate_errors():
            for collection, scope in CLEANUP_ORDER:
                counts[collection.value] = await self.collection(collection).count_documents(
                    {"session_id": session_id, **scope}
                )
        return counts

    @staticmethod
    def _eligible(*, now: datetime, environment: str | None = None) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "expires_at": {"$lte": to_bson(now)},
            "status": {"$in": list(TERMINAL_STATES)},
        }
        if environment is not None:
            filters = {"environment": environment, **filters}
        return filters
