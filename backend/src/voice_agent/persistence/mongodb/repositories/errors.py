"""``error_events`` repository and the worker ``ErrorEventRepository`` adapter (docs/02 §12).

A duplicate delivery of the same ``error_id`` is deduplicated; separate
occurrences keep separate IDs. Classification and diagnostics are
immutable; only ``resolution`` changes, checked on ``resolution.revision``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument

from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.domain.error_event import (
    ErrorEventRecord,
    ResolutionAction,
    ResolutionStatus,
    error_event_from_failure,
)
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError, translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    build,
    encode,
    parse,
    scheduled,
)
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import ErrorCursor
from voice_agent.ports.persistence import MAX_QUERY_LIMIT
from voice_agent.ports.repositories import RevisionConflictError


class MongoErrorEventStore(MongoRepository):
    async def record(self, error: ErrorEventRecord) -> bool:
        anchor = await self.require_session_anchor(error.session_id)
        try:
            async with translate_errors():
                await self.collection(Collection.ERROR_EVENTS).insert_one(
                    scheduled(encode(error), anchor)
                )
        except IndexedDuplicateKeyError:
            return False
        return True

    async def get(self, error_id: str) -> ErrorEventRecord | None:
        async with translate_errors():
            raw = await self.collection(Collection.ERROR_EVENTS).find_one({"error_id": error_id})
        return None if raw is None else parse(ErrorEventRecord, raw)

    async def list_for_session(
        self, session_id: str, *, limit: int, after: datetime | None = None
    ) -> Sequence[ErrorEventRecord]:
        filters: dict[str, Any] = {"session_id": session_id}
        if after is not None:
            filters["occurred_at"] = {"$gt": to_bson(after)}
        return await self._list(filters, [("occurred_at", 1)], limit)

    async def list_session_errors(
        self, session_id: str, *, limit: int, after: ErrorCursor | None = None
    ) -> Sequence[ErrorEventRecord]:
        """Control-API page ordered by ``(occurred_at, error_id)`` (``ix_session_errors``)."""
        filters: dict[str, Any] = {"session_id": session_id}
        if after is not None:
            stamp = to_bson(after.occurred_at)
            filters["$or"] = [
                {"occurred_at": {"$gt": stamp}},
                {"occurred_at": stamp, "error_id": {"$gt": after.error_id}},
            ]
        # One extra row past the page maximum lets the caller detect a next page.
        sort = [("occurred_at", 1), ("error_id", 1)]
        return await self._list(filters, sort, limit, maximum=MAX_QUERY_LIMIT + 1)

    async def list_by_fingerprint(
        self, fingerprint: str, *, limit: int
    ) -> Sequence[ErrorEventRecord]:
        return await self._list(
            {"error_fingerprint": fingerprint}, [("occurred_at", DESCENDING)], limit
        )

    async def resolution_queue(
        self, environment: str, status: ResolutionStatus, *, limit: int
    ) -> Sequence[ErrorEventRecord]:
        return await self._list(
            {"environment": environment, "resolution.status": status.value},
            [("severity", 1), ("occurred_at", 1)],
            limit,
        )

    async def resolve(
        self,
        error_id: str,
        *,
        expected_revision: int,
        status: ResolutionStatus,
        action: ResolutionAction | None,
        now: datetime,
        note: str | None = None,
    ) -> ErrorEventRecord:
        current = await self.get(error_id)
        if current is None or current.resolution.revision != expected_revision:
            raise RevisionConflictError("error resolution revision changed")
        resolved = current.resolve(status=status, action=action, now=now, note=note)
        changes: dict[str, Any] = {
            "resolution": encode(resolved.resolution),
            "updated_at": to_bson(now),
        }
        update: dict[str, Any] = {"$set": changes}
        if resolved.resolved_at is not None:
            changes["resolved_at"] = to_bson(resolved.resolved_at)
        async with translate_errors():
            stored = await self.collection(Collection.ERROR_EVENTS).find_one_and_update(
                {"error_id": error_id, "resolution.revision": expected_revision},
                update,
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise RevisionConflictError("error resolution revision changed")
        return parse(ErrorEventRecord, stored)

    async def _list(
        self,
        filters: dict[str, Any],
        sort: list[tuple[str, int]],
        limit: int,
        *,
        maximum: int = MAX_QUERY_LIMIT,
    ) -> list[ErrorEventRecord]:
        bounded = bounded_limit(limit, maximum)
        async with translate_errors():
            rows = await (
                self.collection(Collection.ERROR_EVENTS)
                .find(filters, sort=sort, limit=bounded)
                .to_list(length=bounded)
            )
        return [parse(ErrorEventRecord, row) for row in rows]


class MongoErrorEventRepository:
    """Worker port adapter: maps a normalized failure to the durable record."""

    def __init__(self, persistence: MongoPersistence, *, context: WriteContext, clock: Clock):
        self._store = MongoErrorEventStore(persistence)
        self._context = context
        self._clock = clock

    async def add(self, error_id: str, failure: NormalizedFailure) -> None:
        record = build(
            ErrorEventRecord,
            error_event_from_failure,
            error_id=error_id,
            failure=failure,
            correlation_id=self._context.correlation_id,
            environment=self._context.environment,
            recorded_at=self._clock.utc_now(),
        )
        await self._store.record(record)
