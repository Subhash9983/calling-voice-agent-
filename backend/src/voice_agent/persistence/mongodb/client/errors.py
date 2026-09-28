"""Driver-failure normalization (docs/02 §24 "Failure behaviour").

PyMongo exceptions never leave the persistence layer. Validation rejection
becomes :class:`PersistenceRejectedError` (normalized ``persistence_failed``);
connectivity, timeout, and authorization failures become
:class:`StoreUnavailableError`. The original exception chain is dropped so
driver messages (hosts, rejected documents) never reach callers or logs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from pymongo import errors as pymongo_errors

from voice_agent.ports.control_plane import DuplicateKeyError, StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError

DOCUMENT_VALIDATION_FAILURE: Final = 121


class IndexedDuplicateKeyError(DuplicateKeyError):
    """Duplicate key carrying only the violated index key fields (never values)."""

    def __init__(self, key_fields: tuple[str, ...]) -> None:
        super().__init__("duplicate key on " + ",".join(key_fields))
        self.key_fields = key_fields


def duplicate_key_fields(details: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not details:
        return ()
    pattern = details.get("keyPattern")
    return tuple(str(key) for key in pattern) if isinstance(pattern, Mapping) else ()


def _translate(exc: pymongo_errors.PyMongoError) -> Exception:
    if isinstance(exc, pymongo_errors.DuplicateKeyError):
        return IndexedDuplicateKeyError(duplicate_key_fields(exc.details))
    if isinstance(exc, pymongo_errors.BulkWriteError):
        return _translate_bulk(exc)
    if isinstance(exc, pymongo_errors.OperationFailure):
        if exc.code == DOCUMENT_VALIDATION_FAILURE:
            return PersistenceRejectedError("document failed collection validation")
        return StoreUnavailableError("database operation failed")
    return StoreUnavailableError("database unavailable")


def _translate_bulk(exc: pymongo_errors.BulkWriteError) -> Exception:
    errors = exc.details.get("writeErrors", []) if exc.details else []
    first = errors[0] if errors else {}
    code = first.get("code")
    if code == 11000:
        return IndexedDuplicateKeyError(duplicate_key_fields(first))
    if code == DOCUMENT_VALIDATION_FAILURE:
        return PersistenceRejectedError("document failed collection validation")
    return StoreUnavailableError("database bulk write failed")


@asynccontextmanager
async def translate_errors() -> AsyncIterator[None]:
    try:
        yield
    except pymongo_errors.PyMongoError as exc:
        raise _translate(exc) from None
