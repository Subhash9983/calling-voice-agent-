"""``conversation_turns`` / ``provider_operations`` writers and timeline reads (docs/02 §7-§8).

Writers check references on first insert (the session exists; an
operation's turn belongs to the same session) and reject stale revisions:
an update applies only while the stored revision is not newer. Reads are
bounded pages on the approved session/sequence and session/time indexes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError, translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.timeline import (
    ConversationTurnDocument,
    ProviderOperationDocument,
    WriteContext,
    operation_document,
    operation_from_document,
    turn_document,
    turn_from_document,
)
from voice_agent.persistence.mongodb.repositories.base import (
    Document,
    MongoRepository,
    build,
    encode,
    parse,
)
from voice_agent.persistence.mongodb.repositories.events import list_event_records
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import (
    EventQuery,
    EventRecord,
    OperationQuery,
    OperationView,
    TurnView,
)
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

MAX_SESSION_CHILDREN: Final = 1000
_IMMUTABLE: Final = frozenset(
    {
        "turn_id",
        "operation_id",
        "session_id",
        "sequence_number",
        "logical_request_id",
        "attempt_number",
        "correlation_id",
        "agent_config_id",
        "environment",
        "schema_version",
        "created_at",
    }
)
_TURN_OPTIONAL: Final = ("response_finish_reason",)
_OPERATION_OPTIONAL: Final = ("completed_at", "failure_summary")


class _RevisionedWriter(MongoRepository):
    collection_name: Collection
    key: str
    revision_field: str
    optional_fields: tuple[str, ...]

    def __init__(self, persistence: MongoPersistence, *, context: WriteContext, clock: Clock):
        super().__init__(persistence)
        self._context = context
        self._clock = clock

    async def _save(self, key_value: str, revision: int, encoded: Document) -> bool:
        """Update in place; ``False`` when no stored document exists yet."""
        mutable = {k: v for k, v in encoded.items() if k not in _IMMUTABLE}
        unset = {name: "" for name in self.optional_fields if name not in encoded}
        update: dict[str, Any] = {"$set": mutable}
        if unset:
            update["$unset"] = unset
        filters = {
            self.key: key_value,
            "session_id": encoded["session_id"],
            self.revision_field: {"$lte": revision},
        }
        async with translate_errors():
            result = await self.collection(self.collection_name).update_one(filters, update)
            if result.matched_count == 1:
                return True
            existing = await self.collection(self.collection_name).count_documents(
                {self.key: key_value}, limit=1
            )
        if existing:
            raise RevisionConflictError("stale or mismatched child revision")
        return False

    async def _insert(self, encoded: Document) -> None:
        try:
            async with translate_errors():
                await self.collection(self.collection_name).insert_one(encoded)
        except IndexedDuplicateKeyError:
            raise RevisionConflictError("concurrent child insert") from None

    async def _require_session(self, session_id: str) -> None:
        async with translate_errors():
            found = await self.collection(Collection.VOICE_SESSIONS).count_documents(
                {"session_id": session_id}, limit=1
            )
        if not found:
            raise ReferenceNotFoundError("the parent session does not exist")


class MongoTurnRepository(_RevisionedWriter):
    collection_name = Collection.CONVERSATION_TURNS
    key = "turn_id"
    revision_field = "status_revision"
    optional_fields = _TURN_OPTIONAL

    async def get(self, turn_id: str) -> ConversationTurn | None:
        async with translate_errors():
            raw = await self.collection(self.collection_name).find_one({"turn_id": turn_id})
        return None if raw is None else turn_from_document(parse(ConversationTurnDocument, raw))

    async def save(self, turn: ConversationTurn) -> None:
        now = self._clock.utc_now()
        document = build(
            ConversationTurnDocument,
            turn_document,
            turn,
            self._context,
            created_at=now,
            updated_at=now,
        )
        encoded = encode(document)
        if await self._save(turn.turn_id, turn.status_revision, encoded):
            return
        await self._require_session(turn.session_id)
        await self._insert(encoded)

    async def list_for_session(self, session_id: str) -> Sequence[ConversationTurn]:
        async with translate_errors():
            rows = await (
                self.collection(self.collection_name)
                .find(
                    {"session_id": session_id},
                    sort=[("sequence_number", 1)],
                    limit=MAX_SESSION_CHILDREN,
                )
                .to_list(length=MAX_SESSION_CHILDREN)
            )
        return [turn_from_document(parse(ConversationTurnDocument, row)) for row in rows]


class MongoOperationRepository(_RevisionedWriter):
    collection_name = Collection.PROVIDER_OPERATIONS
    key = "operation_id"
    revision_field = "status_revision"
    optional_fields = _OPERATION_OPTIONAL

    async def get(self, operation_id: str) -> ProviderOperation | None:
        async with translate_errors():
            raw = await self.collection(self.collection_name).find_one(
                {"operation_id": operation_id}
            )
        if raw is None:
            return None
        return operation_from_document(parse(ProviderOperationDocument, raw))

    async def save(self, operation: ProviderOperation) -> None:
        now = self._clock.utc_now()
        document = build(
            ProviderOperationDocument,
            operation_document,
            operation,
            self._context,
            created_at=now,
            updated_at=now,
        )
        encoded = encode(document)
        if await self._save(operation.operation_id, operation.status_revision, encoded):
            return
        await self._require_session(operation.session_id)
        if operation.turn_id is not None:
            await self._require_turn(operation.session_id, operation.turn_id)
        await self._insert(encoded)

    async def _require_turn(self, session_id: str, turn_id: str) -> None:
        async with translate_errors():
            found = await self.collection(Collection.CONVERSATION_TURNS).count_documents(
                {"turn_id": turn_id, "session_id": session_id}, limit=1
            )
        if not found:
            raise ReferenceNotFoundError("the operation's turn is not in its session")

    async def list_for_session(self, session_id: str) -> Sequence[ProviderOperation]:
        async with translate_errors():
            rows = await (
                self.collection(self.collection_name)
                .find(
                    {"session_id": session_id},
                    sort=[("created_at", 1), ("attempt_number", 1), ("operation_id", 1)],
                    limit=MAX_SESSION_CHILDREN,
                )
                .to_list(length=MAX_SESSION_CHILDREN)
            )
        return [operation_from_document(parse(ProviderOperationDocument, row)) for row in rows]


class MongoSessionTimelineReader(MongoRepository):
    """Control-plane ``SessionTimelineReader`` over the approved indexes."""

    async def list_turns(
        self, session_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[TurnView]:
        bounded = min(limit, MAX_QUERY_LIMIT + 1)
        async with translate_errors():
            rows = await (
                self.collection(Collection.CONVERSATION_TURNS)
                .find(
                    {"session_id": session_id, "sequence_number": {"$gt": after_sequence}},
                    sort=[("sequence_number", 1)],
                    limit=bounded,
                )
                .to_list(length=bounded)
            )
        return [_turn_view(parse(ConversationTurnDocument, row)) for row in rows]

    async def get_turn(self, session_id: str, turn_id: str) -> TurnView | None:
        async with translate_errors():
            raw = await self.collection(Collection.CONVERSATION_TURNS).find_one(
                {"turn_id": turn_id, "session_id": session_id}
            )
        return None if raw is None else _turn_view(parse(ConversationTurnDocument, raw))

    async def list_events(self, query: EventQuery) -> Sequence[EventRecord]:
        return await list_event_records(self, query)

    async def list_operations(self, query: OperationQuery) -> Sequence[OperationView]:
        filters: dict[str, Any] = {"session_id": query.session_id}
        for field, value in (
            ("turn_id", query.turn_id),
            ("component", query.component.value if query.component else None),
            ("status", query.status.value if query.status else None),
        ):
            if value is not None:
                filters[field] = value
        if query.after is not None:
            after = to_bson(query.after.created_at)
            filters["$or"] = [
                {"created_at": {"$gt": after}},
                {"created_at": after, "operation_id": {"$gt": query.after.operation_id}},
            ]
        bounded = min(query.limit, MAX_QUERY_LIMIT + 1)
        async with translate_errors():
            rows = await (
                self.collection(Collection.PROVIDER_OPERATIONS)
                .find(filters, sort=[("created_at", 1), ("operation_id", 1)], limit=bounded)
                .to_list(length=bounded)
            )
        return [_operation_view(parse(ProviderOperationDocument, row)) for row in rows]

    async def get_operation(self, session_id: str, operation_id: str) -> OperationView | None:
        async with translate_errors():
            raw = await self.collection(Collection.PROVIDER_OPERATIONS).find_one(
                {"operation_id": operation_id, "session_id": session_id}
            )
        return None if raw is None else _operation_view(parse(ProviderOperationDocument, raw))


def _turn_view(doc: ConversationTurnDocument) -> TurnView:
    return TurnView(
        turn=turn_from_document(doc), created_at=doc.created_at, updated_at=doc.updated_at
    )


def _operation_view(doc: ProviderOperationDocument) -> OperationView:
    return OperationView(operation=operation_from_document(doc), created_at=doc.created_at)
