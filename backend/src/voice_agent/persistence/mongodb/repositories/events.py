"""Shared atomic event-sequence allocator and ``session_events`` stores (docs/02 §9).

API, worker, and maintenance writers all obtain ``sequence_number`` from one
``findOneAndUpdate`` with ``$inc: {event_sequence_counter: 1}`` on the
session, returning the updated value; the increment changes neither
``state_revision`` nor ``updated_at``. Numbers left unused by a failed or
deduplicated write are allowed gaps. The same round trip returns the
session's retention anchor: an ordinary-class event appended after the
session became terminal (``session.ended``/``session.failed`` and other late
evidence) receives ``expires_at = ended_at + 30 days`` itself, because the
one-time terminal propagation has already run (docs/02 §20; WP11). ``uq_event_id`` deduplicates
deliveries and ``uq_session_event_sequence`` guarantees unique ordering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pymongo import ReturnDocument

from voice_agent.contracts.events import EventEnvelope, EventVisibility
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError, translate_errors
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import (
    EventWriteContext,
    SessionEventDocument,
    event_document,
    record_from_event_document,
    severity_for,
)
from voice_agent.persistence.mongodb.documents.session import TERMINAL_STATES
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    build,
    encode,
    parse,
)
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import EventQuery, EventRecord
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

MAX_SESSION_EVENTS_READ: Final = 5000


@dataclass(frozen=True, slots=True)
class SequenceAllocation:
    sequence_number: int
    # ``ended_at`` of a terminal session (the retention anchor), else ``None``.
    retention_anchor: datetime | None


class MongoEventSequenceAllocator(MongoRepository):
    async def next_sequence(self, session_id: str) -> int:
        return (await self.allocate(session_id)).sequence_number

    async def allocate(self, session_id: str) -> SequenceAllocation:
        async with translate_errors():
            stored = await self.collection(Collection.VOICE_SESSIONS).find_one_and_update(
                {"session_id": session_id},
                {"$inc": {"event_sequence_counter": 1}},
                projection={"event_sequence_counter": 1, "status": 1, "ended_at": 1, "_id": 0},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise ReferenceNotFoundError("events require an existing session")
        anchor = stored.get("ended_at") if stored.get("status") in TERMINAL_STATES else None
        return SequenceAllocation(int(stored["event_sequence_counter"]), anchor)


class _EventInserter(MongoRepository):
    def __init__(self, persistence: MongoPersistence, *, context: EventWriteContext) -> None:
        super().__init__(persistence)
        self._context = context

    async def _insert(
        self,
        record: EventRecord,
        sequence_number: int,
        retention_anchor: datetime | None = None,
    ) -> bool:
        """Insert one event; ``False`` for a duplicate delivery of the same event ID."""
        document = build(
            SessionEventDocument,
            event_document,
            record,
            sequence_number=sequence_number,
            context=self._context,
            retention_anchor=retention_anchor,
        )
        try:
            async with translate_errors():
                await self.collection(Collection.SESSION_EVENTS).insert_one(encode(document))
        except IndexedDuplicateKeyError as exc:
            if exc.key_fields == ("event_id",):
                return False
            raise RevisionConflictError("duplicate (session_id, sequence_number)") from None
        return True


class MongoSessionEventLog(_EventInserter):
    """Control-plane ``SessionEventLog``: allocates, then appends."""

    def __init__(self, persistence: MongoPersistence, *, context: EventWriteContext) -> None:
        super().__init__(persistence, context=context)
        self._allocator = MongoEventSequenceAllocator(persistence)

    async def append(self, record: EventRecord) -> None:
        allocation = await self._allocator.allocate(record.envelope.session_id)
        await self._insert(record, allocation.sequence_number, allocation.retention_anchor)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        if not event_ids:
            return frozenset()
        async with translate_errors():
            rows = await (
                self.collection(Collection.SESSION_EVENTS)
                .find(
                    {"session_id": session_id, "event_id": {"$in": list(event_ids)}},
                    projection={"event_id": 1, "_id": 0},
                    limit=len(event_ids),
                )
                .to_list(length=len(event_ids))
            )
        return frozenset(str(row["event_id"]) for row in rows)


class MongoWorkerEventRepository(_EventInserter):
    """Worker ``SessionEventRepository``: the sequence was allocated by the writer."""

    def __init__(
        self, persistence: MongoPersistence, *, context: EventWriteContext, clock: Clock
    ) -> None:
        super().__init__(persistence, context=context)
        self._clock = clock

    async def append(self, envelope: EventEnvelope) -> None:
        if envelope.sequence_number is None:
            raise ValueError("durable events require an allocated sequence number")
        record = EventRecord(
            envelope=envelope,
            severity=severity_for(envelope.event_type),
            recorded_at=self._clock.utc_now(),
        )
        await self._insert(record, envelope.sequence_number)

    async def list_for_session(self, session_id: str) -> Sequence[EventEnvelope]:
        async with translate_errors():
            rows = await (
                self.collection(Collection.SESSION_EVENTS)
                .find(
                    {"session_id": session_id},
                    sort=[("sequence_number", 1)],
                    limit=MAX_SESSION_EVENTS_READ,
                )
                .to_list(length=MAX_SESSION_EVENTS_READ)
            )
        return [record_from_event_document(parse(SessionEventDocument, r)).envelope for r in rows]


async def list_event_records(repository: MongoRepository, query: EventQuery) -> list[EventRecord]:
    """Bounded timeline page ordered by ``sequence_number`` (``uq_session_event_sequence``)."""
    filters: dict[str, Any] = {
        "session_id": query.session_id,
        "sequence_number": {"$gt": query.after_sequence},
    }
    if query.category is not None:
        filters["category"] = query.category.value
    if query.severity is not None:
        filters["severity"] = query.severity.value
    if query.browser_safe_only:
        filters["visibility"] = EventVisibility.BROWSER_SAFE.value
    limit = min(query.limit, MAX_QUERY_LIMIT + 1)
    async with translate_errors():
        rows = await (
            repository.collection(Collection.SESSION_EVENTS)
            .find(filters, sort=[("sequence_number", 1)], limit=limit)
            .to_list(length=limit)
        )
    return [record_from_event_document(parse(SessionEventDocument, row)) for row in rows]
