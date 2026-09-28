"""Opaque, kind-tagged pagination cursors (docs/04 §2, §8, §10; docs/02 §18).

A cursor is base64url JSON naming its endpoint kind, so a cursor from one
listing cannot be replayed against another. Malformed cursors are rejected
as ``422 VALIDATION_FAILED`` on ``query.cursor`` without echoing the value.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from voice_agent.contracts.base import CANONICAL_UUID_LENGTH
from voice_agent.control_api.errors import ApiError, validation_failed
from voice_agent.ports.control_plane import OperationCursor, SessionCursor

CURSOR_FIELD = "query.cursor"
CURSOR_INVALID = "cursor_invalid"


def _invalid() -> ApiError:
    return validation_failed(CURSOR_FIELD, CURSOR_INVALID)


def encode_cursor(kind: str, values: Mapping[str, str | int]) -> str:
    raw = json.dumps({"k": kind, "v": dict(values)}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(kind: str, cursor: str) -> Mapping[str, Any] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not isinstance(data, dict) or data.get("k") != kind or not isinstance(data.get("v"), dict):
        return None
    values: Mapping[str, Any] = data["v"]
    return values


def decode_sequence_cursor(kind: str, cursor: str | None) -> int:
    if cursor is None:
        return 0
    values = _decode(kind, cursor)
    sequence = None if values is None else values.get("s")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise _invalid()
    return sequence


def _timestamp_and_id(values: Mapping[str, Any] | None) -> tuple[datetime, str] | None:
    if values is None:
        return None
    stamp, identifier = values.get("c"), values.get("i")
    if not isinstance(stamp, str) or not isinstance(identifier, str):
        return None
    if len(identifier) != CANONICAL_UUID_LENGTH:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return (parsed, identifier) if parsed.tzinfo is not None else None


def encode_session_cursor(created_at: datetime, session_id: str) -> str:
    return encode_cursor("sessions", {"c": created_at.isoformat(), "i": session_id})


def decode_session_cursor(cursor: str | None) -> SessionCursor | None:
    if cursor is None:
        return None
    parsed = _timestamp_and_id(_decode("sessions", cursor))
    if parsed is None:
        raise _invalid()
    return SessionCursor(created_at=parsed[0], session_id=parsed[1])


def encode_operation_cursor(created_at: datetime, operation_id: str) -> str:
    return encode_cursor("operations", {"c": created_at.isoformat(), "i": operation_id})


def decode_operation_cursor(cursor: str | None) -> OperationCursor | None:
    if cursor is None:
        return None
    parsed = _timestamp_and_id(_decode("operations", cursor))
    if parsed is None:
        raise _invalid()
    return OperationCursor(created_at=parsed[0], operation_id=parsed[1])
