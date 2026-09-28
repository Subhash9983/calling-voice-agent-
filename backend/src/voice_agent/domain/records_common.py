"""Shared durable-record vocabulary (docs/02 §4, §24; docs/16 §4).

Record models are frozen, forbid unknown fields, and never echo input
values in validation errors. Optional values are omitted when unavailable;
money and fractional measurements are ``Decimal``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from voice_agent.contracts.base import PreciseDecimal

MAX_SAFE_NOTE_CHARS = 2000
MAX_DESCRIPTION_CHARS = 500
MAX_NAME_CHARS = 100
MAX_SAFE_MESSAGE_CHARS = 1000
MAX_BROWSER_MESSAGE_CHARS = 500
MAX_TAGS = 20


class RecordModel(BaseModel):
    """Frozen, extra-forbidding durable record whose errors never include input."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, hide_input_in_errors=True
    )


class RedactionStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    REDACTED = "redacted"
    FAILED = "failed"


class Visibility(StrEnum):
    INTERNAL = "internal"
    BROWSER_SUMMARY = "browser_summary"
    RESTRICTED_PRICING = "restricted_pricing"


def _canonical_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode())


def bounded_container(max_bytes: int, max_keys: int) -> AfterValidator:
    """Bounded allowlist container: key count and canonical UTF-8 size (docs/02 §24)."""

    def check(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(value) > max_keys:
            raise ValueError(f"container allows at most {max_keys} top-level keys")
        if _canonical_size(value) > max_bytes:
            raise ValueError(f"container exceeds {max_bytes} bytes")
        return value

    return AfterValidator(check)


def unique_items[T](value: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(value)) != len(value):
        raise ValueError("items must be unique")
    return value


SafeNote = Annotated[str, Field(min_length=1, max_length=MAX_SAFE_NOTE_CHARS)]
SafeMessage = Annotated[str, Field(min_length=1, max_length=MAX_SAFE_MESSAGE_CHARS)]
Revision = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
Checksum = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
SafeDetails = Annotated[dict[str, JsonValue], bounded_container(8 * 1024, 30)]
Money = PreciseDecimal
