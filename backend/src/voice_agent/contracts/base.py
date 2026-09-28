"""Shared strict model base and canonical scalar types (docs/01 §4, docs/03 §10).

Every contract is a frozen Pydantic model that forbids unknown fields.
Identifiers are canonical lowercase UUID strings and timestamps are UTC.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

CANONICAL_UUID_LENGTH = 36
MAX_IDENTIFIER_LENGTH = 256
MAX_LABEL_LENGTH = 50
MAX_FRACTIONAL_DIGITS = 12


class StrictModel(BaseModel):
    """Immutable contract model that rejects unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


def _require_canonical_uuid(value: str) -> str:
    if len(value) != CANONICAL_UUID_LENGTH:
        raise ValueError("identifier must be a 36-character canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("identifier must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ValueError("identifier must use the canonical lowercase UUID form")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _require_precise_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("decimal value must be finite")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > MAX_FRACTIONAL_DIGITS:
        raise ValueError(f"decimal value allows at most {MAX_FRACTIONAL_DIGITS} fractional digits")
    return value


def _require_finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("decimal value must be finite")
    return value


def _require_non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("decimal value must not be negative")
    return value


def _require_finite_probability(value: float) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("probability must be within [0, 1]")
    return value


CanonicalId = Annotated[str, AfterValidator(_require_canonical_uuid)]
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]
PreciseDecimal = Annotated[Decimal, AfterValidator(_require_precise_decimal)]
NonNegativeDecimal = Annotated[
    Decimal, AfterValidator(_require_precise_decimal), AfterValidator(_require_non_negative)
]
# Derived quantities (e.g. seconds / 60) may exceed 12 fractional digits;
# they stay exact and are rounded only at the reporting boundary.
FiniteDecimal = Annotated[Decimal, AfterValidator(_require_finite)]
FiniteNonNegativeDecimal = Annotated[
    Decimal, AfterValidator(_require_finite), AfterValidator(_require_non_negative)
]
PositiveDecimal = Annotated[Decimal, Field(gt=0), AfterValidator(_require_precise_decimal)]
Probability = Annotated[float, AfterValidator(_require_finite_probability)]
ExternalIdentifier = Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)]
ShortLabel = Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LENGTH)]
Generation = Annotated[int, Field(ge=0)]
WorkerGeneration = Annotated[int, Field(ge=1)]
MonotonicMs = Annotated[int, Field(ge=0)]
