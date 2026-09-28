"""``user_feedback`` repository (docs/02 §11, §18-§19).

Duplicate ``client_submission_id`` values never create duplicate feedback;
every referenced turn/operation must belong to the stated session; only the
``review`` section changes after creation, checked on ``review_revision``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument

from voice_agent.domain.feedback import FeedbackRecord, ResolutionCode, ReviewStatus
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.feedback import (
    UserFeedbackDocument,
    feedback_document,
    feedback_from_document,
)
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    build,
    encode,
    parse,
)
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError


class MongoFeedbackRepository(MongoRepository):
    async def get_by_client_submission_id(self, client_submission_id: str) -> FeedbackRecord | None:
        return await self._find_one({"client_submission_id": client_submission_id})

    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        return await self._find_one({"feedback_id": feedback_id})

    async def insert(self, record: FeedbackRecord) -> None:
        document = build(UserFeedbackDocument, feedback_document, record)
        await self._check_references(document)
        async with translate_errors():
            await self.collection(Collection.USER_FEEDBACK).insert_one(encode(document))

    async def list_for_session(self, session_id: str, *, limit: int) -> Sequence[FeedbackRecord]:
        return await self._list(
            {"session_id": session_id}, sort=[("created_at", DESCENDING)], limit=limit
        )

    async def review_queue(
        self, environment: str, status: ReviewStatus, *, limit: int
    ) -> Sequence[FeedbackRecord]:
        return await self._list(
            {"environment": environment, "review.status": status.value},
            sort=[("created_at", 1)],
            limit=limit,
        )

    async def record_review(
        self,
        feedback_id: str,
        *,
        expected_review_revision: int,
        status: ReviewStatus,
        reviewer: str,
        now: datetime,
        resolution_code: ResolutionCode | None = None,
        note: str | None = None,
    ) -> FeedbackRecord:
        current = await self.get(feedback_id)
        if current is None or current.review.review_revision != expected_review_revision:
            raise RevisionConflictError("feedback review revision changed")
        reviewed = current.reviewed(
            status=status, reviewer=reviewer, now=now, resolution_code=resolution_code, note=note
        )
        async with translate_errors():
            stored = await self.collection(Collection.USER_FEEDBACK).find_one_and_update(
                {"feedback_id": feedback_id, "review.review_revision": expected_review_revision},
                {"$set": {"review": encode(reviewed.review), "updated_at": to_bson(now)}},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise RevisionConflictError("feedback review revision changed")
        return feedback_from_document(parse(UserFeedbackDocument, stored))

    async def _check_references(self, document: UserFeedbackDocument) -> None:
        checks: list[tuple[Collection, dict[str, Any]]] = [
            (Collection.VOICE_SESSIONS, {"session_id": document.session_id})
        ]
        if document.turn_id is not None:
            checks.append(
                (
                    Collection.CONVERSATION_TURNS,
                    {"turn_id": document.turn_id, "session_id": document.session_id},
                )
            )
        if document.operation_id is not None:
            operation: dict[str, Any] = {
                "operation_id": document.operation_id,
                "session_id": document.session_id,
            }
            if document.turn_id is not None:
                operation["turn_id"] = document.turn_id
            checks.append((Collection.PROVIDER_OPERATIONS, operation))
        async with translate_errors():
            for collection, filters in checks:
                if not await self.collection(collection).count_documents(filters, limit=1):
                    raise ReferenceNotFoundError("a feedback target is not in the session")

    async def _find_one(self, filters: dict[str, Any]) -> FeedbackRecord | None:
        async with translate_errors():
            raw = await self.collection(Collection.USER_FEEDBACK).find_one(filters)
        return None if raw is None else feedback_from_document(parse(UserFeedbackDocument, raw))

    async def _list(
        self, filters: dict[str, Any], *, sort: list[tuple[str, int]], limit: int
    ) -> list[FeedbackRecord]:
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        async with translate_errors():
            rows = await (
                self.collection(Collection.USER_FEEDBACK)
                .find(filters, sort=sort, limit=bounded)
                .to_list(length=bounded)
            )
        return [feedback_from_document(parse(UserFeedbackDocument, row)) for row in rows]
