"""``voice_sessions`` control-plane repository (docs/02 §6, §18-§19; docs/04 §6-§9).

- ``replace`` is a ``state_revision`` compare-and-set that writes only the
  record-owned fields (never join-token evidence, the event counter, worker
  assignment, or recovery authorization) and recomputes ``next_reconcile_at``;
- ``record_join_token_request`` is atomic: a positional update for a replay
  or ``$push`` with ``$sort``/``$slice`` for a new entry, conditioned on the
  expected ``state_revision``;
- reaching a terminal state propagates the retention anchor to children.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pymongo import DESCENDING

from voice_agent.domain.control_session import (
    MAX_JOIN_TOKEN_REQUESTS,
    JoinTokenOutcome,
    JoinTokenRequestEntry,
    SessionRecord,
)
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import (
    VoiceSessionDocument,
    mutable_fields,
    record_from_document,
    session_document,
)
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    build,
    encode,
    literal_set_stage,
    parse,
    reconcile_stage,
    unset_stage,
)
from voice_agent.persistence.mongodb.repositories.retention import SessionExpiryPropagator
from voice_agent.ports.control_plane import SessionListQuery
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

MAX_JOIN_RACE_RETRIES: Final = 2


class MongoSessionRecordRepository(MongoRepository):
    def __init__(self, persistence: MongoPersistence) -> None:
        super().__init__(persistence)
        self._expiry = SessionExpiryPropagator(persistence)

    async def ping(self) -> None:
        await self._persistence.ping()

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self._find_one({"session_id": session_id})

    async def get_by_client_request_id(self, client_request_id: str) -> SessionRecord | None:
        return await self._find_one({"client_request_id": client_request_id})

    async def insert(self, record: SessionRecord) -> None:
        document = build(VoiceSessionDocument, session_document, record)
        await self._require_config(record.agent_config_id, record.config_checksum)
        async with translate_errors():
            await self.collection(Collection.VOICE_SESSIONS).insert_one(encode(document))

    async def replace(self, record: SessionRecord, *, expected_revision: int) -> None:
        to_set, to_unset = mutable_fields(record)
        pipeline = [
            literal_set_stage(to_set),
            *unset_stage(to_unset),
            reconcile_stage(),
        ]
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).update_one(
                {"session_id": record.session_id, "state_revision": expected_revision}, pipeline
            )
        if result.matched_count == 0:
            raise RevisionConflictError("session revision changed")
        if record.is_terminal:
            await self._expiry.propagate_session_expiry(record.session_id)

    async def record_join_token_request(
        self,
        session_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
        fingerprint: str,
        now: datetime,
    ) -> JoinTokenOutcome:
        base = {"session_id": session_id, "state_revision": expected_revision}
        for _attempt in range(MAX_JOIN_RACE_RETRIES):
            if await self._replay_join(base, client_request_id, fingerprint, now):
                return JoinTokenOutcome.REPLAYED
            if await self._push_join(base, client_request_id, fingerprint, now):
                return JoinTokenOutcome.RECORDED
            outcome = await self._diagnose_join(
                session_id, expected_revision, client_request_id, fingerprint
            )
            if outcome is not None:
                return outcome
        raise RevisionConflictError("join-token evidence changed concurrently")

    async def _replay_join(
        self, base: dict[str, Any], client_request_id: str, fingerprint: str, now: datetime
    ) -> bool:
        match = {"client_request_id": client_request_id, "fingerprint": fingerprint}
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).update_one(
                {**base, "join_token_requests": {"$elemMatch": match}},
                {
                    "$set": {
                        "join_token_requests.$.last_issued_at": to_bson(now),
                        "updated_at": to_bson(now),
                    },
                    "$inc": {"join_token_requests.$.issue_count": 1},
                },
            )
        return result.matched_count == 1

    async def _push_join(
        self, base: dict[str, Any], client_request_id: str, fingerprint: str, now: datetime
    ) -> bool:
        entry = JoinTokenRequestEntry(
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            first_requested_at=now,
            last_issued_at=now,
            issue_count=1,
        )
        push = {
            "$each": [encode(entry)],
            "$sort": {"last_issued_at": 1},
            "$slice": -MAX_JOIN_TOKEN_REQUESTS,
        }
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).update_one(
                {**base, "join_token_requests.client_request_id": {"$ne": client_request_id}},
                {"$push": {"join_token_requests": push}, "$set": {"updated_at": to_bson(now)}},
            )
        return result.matched_count == 1

    async def _diagnose_join(
        self, session_id: str, expected_revision: int, client_request_id: str, fingerprint: str
    ) -> JoinTokenOutcome | None:
        """Why neither atomic write matched; ``None`` means a concurrent insert raced us."""
        async with translate_errors():
            raw = await self.collection(Collection.VOICE_SESSIONS).find_one(
                {"session_id": session_id},
                projection={"state_revision": 1, "join_token_requests": 1, "_id": 0},
            )
        if raw is None or raw.get("state_revision") != expected_revision:
            raise RevisionConflictError("session revision changed")
        entries = raw.get("join_token_requests") or []
        match = next((e for e in entries if e.get("client_request_id") == client_request_id), None)
        if match is not None and match.get("fingerprint") != fingerprint:
            return JoinTokenOutcome.FINGERPRINT_CONFLICT
        return None

    async def list_page(self, query: SessionListQuery) -> Sequence[SessionRecord]:
        filters: dict[str, Any] = {"environment": query.environment}
        if query.status is not None:
            filters["status"] = query.status.value
        if query.agent_config_id is not None:
            filters["agent_config_id"] = query.agent_config_id
        created: dict[str, Any] = {}
        if query.created_before is not None:
            created["$lt"] = to_bson(query.created_before)
        if created:
            filters["created_at"] = created
        if query.after is not None:
            after = to_bson(query.after.created_at)
            filters["$or"] = [
                {"created_at": {"$lt": after}},
                {"created_at": after, "session_id": {"$lt": query.after.session_id}},
            ]
        limit = min(query.limit, MAX_QUERY_LIMIT + 1)
        async with translate_errors():
            rows = await (
                self.collection(Collection.VOICE_SESSIONS)
                .find(
                    filters,
                    sort=[("created_at", DESCENDING), ("session_id", DESCENDING)],
                    limit=limit,
                )
                .to_list(length=limit)
            )
        return [record_from_document(parse(VoiceSessionDocument, row)) for row in rows]

    async def _find_one(self, filters: dict[str, Any]) -> SessionRecord | None:
        async with translate_errors():
            raw = await self.collection(Collection.VOICE_SESSIONS).find_one(filters)
        if raw is None:
            return None
        return record_from_document(parse(VoiceSessionDocument, raw))

    async def _require_config(self, agent_config_id: str, checksum: str) -> None:
        """Reference check: the exact configuration version must exist (docs/02 §17)."""
        async with translate_errors():
            found = await self.collection(Collection.AGENT_CONFIGS).find_one(
                {"agent_config_id": agent_config_id, "config_checksum": checksum},
                projection={"_id": 1},
            )
        if found is None:
            raise ReferenceNotFoundError("the session's agent configuration is not stored")
