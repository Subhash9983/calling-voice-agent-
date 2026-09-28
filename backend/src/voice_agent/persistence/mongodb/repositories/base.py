"""Shared repository plumbing: collection access, parsing, literal pipelines.

Stored documents are re-validated through their strict model on read; a
failure becomes :class:`PersistenceRejectedError` without echoing content.
Values placed into aggregation-pipeline updates are always wrapped in
``$literal`` so data can never be interpreted as a field path or operator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from pymongo.asynchronous.collection import AsyncCollection

from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import (
    document_from_bson,
    model_to_bson,
    to_bson,
)
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import NONTERMINAL_SESSION_STATES
from voice_agent.ports.persistence import PersistenceRejectedError

Document = dict[str, Any]
# Store-computed next reconciliation time: the minimum of every active
# deadline (docs/02 §6). ``$min`` ignores missing fields, and each deadline
# field is present only while it is active.
NEXT_RECONCILE_EXPRESSION: Final = {
    "$min": [
        "$connect_deadline_at",
        "$termination_deadline_at",
        "$idle_deadline_at",
        "$maximum_duration_deadline_at",
        "$worker_assignment.lease_expires_at",
        "$recovery_authorization.expires_at",
        "$recovery_authorization.recovery_deadline_at",
    ]
}


class MongoRepository:
    def __init__(self, persistence: MongoPersistence) -> None:
        self._persistence = persistence

    def collection(self, name: Collection) -> AsyncCollection[Document]:
        collection: AsyncCollection[Document] = self._persistence.database[name.value]
        return collection


def parse[M: BaseModel](model: type[M], raw: Mapping[str, Any]) -> M:
    try:
        return model.model_validate(document_from_bson(raw))
    except ValidationError:
        raise PersistenceRejectedError("stored document failed model validation") from None


def encode(model: BaseModel) -> Document:
    """Validated model -> BSON document (unavailable optional fields omitted)."""
    return model_to_bson(model)


def build[M: BaseModel](model: type[M], factory: Any, *args: Any, **kwargs: Any) -> M:
    """Run a domain->document mapper, normalizing contract violations."""
    try:
        result: M = factory(*args, **kwargs)
    except (ValidationError, ValueError):
        raise PersistenceRejectedError("record cannot be mapped to its stored shape") from None
    if not isinstance(result, model):  # pragma: no cover - mapper contract
        raise PersistenceRejectedError("record mapper returned an unexpected shape")
    return result


def literal(value: Any) -> dict[str, Any]:
    return {"$literal": to_bson(value)}


def literal_set_stage(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {"$set": {key: literal(value) for key, value in fields.items()}}


def reconcile_stage() -> dict[str, Any]:
    """Pipeline stage: recompute the due time while nonterminal, remove it once terminal."""
    return {
        "$set": {
            "next_reconcile_at": {
                "$cond": [
                    {"$in": ["$status", list(NONTERMINAL_SESSION_STATES)]},
                    NEXT_RECONCILE_EXPRESSION,
                    "$$REMOVE",
                ]
            }
        }
    }


def unset_stage(fields: Sequence[str]) -> list[dict[str, Any]]:
    return [{"$unset": list(fields)}] if fields else []


async def in_transaction[T](
    persistence: MongoPersistence, callback: Callable[[Any], Awaitable[T]]
) -> T:
    """Run ``callback(session)`` in one multi-document transaction (retried by the driver).

    If the transaction aborts, none of its writes persist.
    """
    async with translate_errors(), persistence.client.start_session() as session:
        result: T = await session.with_transaction(callback)
    return result


def bounded_limit(limit: int, maximum: int) -> int:
    if limit < 1:
        raise ValueError("query limit must be positive")
    return min(limit, maximum)
