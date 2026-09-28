"""Shared API model base, scalar types, envelopes, and health views (docs/04 §2, §4, §18)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from voice_agent.contracts.base import UtcDatetime

MAX_CURSOR_CHARS = 512
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class ApiModel(BaseModel):
    """Frozen, unknown-field-rejecting API contract whose errors never echo input."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, hide_input_in_errors=True
    )


def _require_iso_string(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not _ISO_TIMESTAMP.match(value):
        raise ValueError("timestamp must be an ISO-8601 UTC string")
    return value


# Rejects numeric epoch coercion; requires timezone-aware UTC.
IsoUtcTimestamp = Annotated[UtcDatetime, BeforeValidator(_require_iso_string)]
Cursor = Annotated[str, Field(min_length=1, max_length=MAX_CURSOR_CHARS)]


class DataEnvelope[T](ApiModel):
    data: T
    request_id: str


class ListEnvelope[T](ApiModel):
    items: tuple[T, ...]
    next_cursor: str | None
    request_id: str


class MutationEnvelope[T](ApiModel):
    data: T
    idempotent_replay: bool
    request_id: str


class NoQueryParameters(ApiModel):
    """Endpoints without query parameters still reject unknown ones."""


class LivenessView(ApiModel):
    status: Literal["ok"] = "ok"


class ReadinessComponentView(ApiModel):
    component: str
    status: str
    reason: str


class ReadinessView(ApiModel):
    status: Literal["ready", "not_ready"]
    components: tuple[ReadinessComponentView, ...]
