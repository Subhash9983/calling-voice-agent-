"""Explicit BSON codecs (docs/02 §24 "Encoding rules"; docs/13 §9).

Python ``Decimal`` <-> ``Decimal128``; aware UTC datetimes (truncated to the
BSON millisecond precision so a written value reads back identically);
enums as their approved strings; tuples/sets as arrays; ``_id`` stays an
internal ObjectId that never leaves this layer. Unsafe container keys
(``$``-prefixed, dotted, or NUL) and non-finite numbers are rejected.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

from bson.codec_options import CodecOptions
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from pydantic import BaseModel

from voice_agent.ports.persistence import PersistenceRejectedError

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
CODEC_OPTIONS: Final[CodecOptions[dict[str, Any]]] = CodecOptions(tz_aware=True, tzinfo=UTC)
INTERNAL_ID: Final = "_id"


def truncate_to_millis(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceRejectedError("persisted timestamps must be timezone-aware UTC")
    utc = value.astimezone(UTC)
    return utc.replace(microsecond=(utc.microsecond // 1000) * 1000)


def _safe_key(key: object) -> str:
    text = str(key)
    if not text or text.startswith("$") or "." in text or "\x00" in text:
        raise PersistenceRejectedError("document key is not storable")
    return text


def _decimal(value: Decimal) -> Decimal128:
    if not value.is_finite():
        raise PersistenceRejectedError("non-finite decimal values are not storable")
    try:
        return Decimal128(value)
    except (InvalidOperation, ArithmeticError) as exc:
        raise PersistenceRejectedError("decimal value exceeds Decimal128 precision") from exc


def to_bson(value: Any) -> Any:
    """Convert a Python value (already dumped from a model) to BSON-ready types."""
    if isinstance(value, Enum):  # before ``str``: a StrEnum is also a str
        return to_bson(value.value)
    if value is None or isinstance(value, bool | str | ObjectId | Decimal128):
        return value
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise PersistenceRejectedError("integer exceeds the int64 range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PersistenceRejectedError("non-finite numbers are not storable")
        return value
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, datetime):
        return truncate_to_millis(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, BaseModel):
        return model_to_bson(value)
    if isinstance(value, Mapping):
        return {_safe_key(key): to_bson(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return [to_bson(item) for item in sorted(value, key=str)]
    if isinstance(value, list | tuple):
        return [to_bson(item) for item in value]
    raise PersistenceRejectedError("value type is not storable")


def model_to_bson(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Dump a record model; unavailable optional values are omitted, never ``null``."""
    dumped = model.model_dump(mode="python", exclude_none=True, exclude=exclude)
    converted = to_bson(dumped)
    if not isinstance(converted, dict):  # pragma: no cover - models always dump mappings
        raise PersistenceRejectedError("record did not serialize to a document")
    return converted


def from_bson(value: Any) -> Any:
    """Convert a stored BSON value back to Python types."""
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, Mapping):
        return {key: from_bson(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_bson(item) for item in value]
    return value


def document_from_bson(document: Mapping[str, Any]) -> dict[str, Any]:
    """Stored document without the internal ``_id`` (never exposed)."""
    converted = from_bson({k: v for k, v in document.items() if k != INTERNAL_ID})
    if not isinstance(converted, dict):  # pragma: no cover - mappings convert to dicts
        raise PersistenceRejectedError("stored value is not a document")
    return converted
