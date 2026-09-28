"""Browser-visible error vocabulary and envelope (docs/04 §19).

Only the allowlisted application codes exist; each maps to exactly one HTTP
status. Messages are fixed, generic, and non-sensitive. Field errors carry a
sanitized location and a Pydantic error *type*, never the submitted value;
an unrecognized key name is reported as ``<unrecognized>`` because a key can
itself be pasted secret material.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

UNRECOGNIZED: Final = "<unrecognized>"
MAX_FIELD_ERRORS = 20
_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_ERROR_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOCATION_ROOTS: frozenset[str] = frozenset({"body", "query", "path", "header"})


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HTTP_STATUS: Mapping[ErrorCode, int] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.AUTHENTICATION_REQUIRED: 401,
        ErrorCode.ACCESS_FORBIDDEN: 403,
        ErrorCode.RESOURCE_NOT_FOUND: 404,
        ErrorCode.INVALID_STATE: 409,
        ErrorCode.REVISION_CONFLICT: 409,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.PAYLOAD_TOO_LARGE: 413,
        ErrorCode.VALIDATION_FAILED: 422,
        ErrorCode.CONSENT_REQUIRED: 422,
        ErrorCode.RATE_LIMITED: 429,
        ErrorCode.PROVIDER_UNAVAILABLE: 502,
        ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
        ErrorCode.INTERNAL_ERROR: 500,
    }
)

DEFAULT_MESSAGES: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: "The request is not supported.",
        ErrorCode.AUTHENTICATION_REQUIRED: "Authentication is required.",
        ErrorCode.ACCESS_FORBIDDEN: "The request is outside the approved access scope.",
        ErrorCode.RESOURCE_NOT_FOUND: "The requested resource was not found.",
        ErrorCode.INVALID_STATE: "The resource state does not allow this operation.",
        ErrorCode.REVISION_CONFLICT: "The resource changed; reload and retry.",
        ErrorCode.IDEMPOTENCY_CONFLICT: "The request ID was already used for a different request.",
        ErrorCode.PAYLOAD_TOO_LARGE: "The request body is too large.",
        ErrorCode.VALIDATION_FAILED: "One or more fields are invalid.",
        ErrorCode.CONSENT_REQUIRED: "An active matching consent grant is required.",
        ErrorCode.RATE_LIMITED: "Too many requests; retry later.",
        ErrorCode.PROVIDER_UNAVAILABLE: "A required provider is unavailable.",
        ErrorCode.DEPENDENCY_UNAVAILABLE: "A required dependency is unavailable.",
        ErrorCode.INTERNAL_ERROR: "An internal error occurred.",
    }
)


class _ErrorModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldError(_ErrorModel):
    field: str
    code: str


class ErrorBody(_ErrorModel):
    code: ErrorCode
    message: str
    retryable: bool
    suggested_action: str | None
    field_errors: tuple[FieldError, ...]


class ErrorEnvelope(_ErrorModel):
    error: ErrorBody
    request_id: str


class ApiError(Exception):
    """A browser-safe failure; the text is fixed and never includes input."""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        retryable: bool = False,
        suggested_action: str | None = None,
        field_errors: Sequence[FieldError] = (),
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.message = message or DEFAULT_MESSAGES[code]
        self.retryable = retryable
        self.suggested_action = suggested_action
        self.field_errors = tuple(field_errors)[:MAX_FIELD_ERRORS]

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.code]

    def body(self) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            suggested_action=self.suggested_action,
            field_errors=self.field_errors,
        )


def error_envelope(error: ApiError, request_id: str) -> dict[str, Any]:
    return ErrorEnvelope(error=error.body(), request_id=request_id).model_dump(mode="json")


def _segment(part: object, known: frozenset[str]) -> str:
    if isinstance(part, int):
        return str(part)
    text = str(part)
    return text if text in known and _NAME.match(text) else UNRECOGNIZED


def safe_location(location: Iterable[object], known: frozenset[str]) -> str:
    parts = list(location)
    if parts and str(parts[0]) in _LOCATION_ROOTS:
        head, rest = str(parts[0]), parts[1:]
        return ".".join([head, *(_segment(part, known) for part in rest)])
    return ".".join(_segment(part, known) for part in parts) or UNRECOGNIZED


def _safe_type(value: object) -> str:
    text = str(value)
    return text if _ERROR_TYPE.match(text) else "invalid"


def field_errors_from_items(
    items: Iterable[Mapping[str, Any]], known: frozenset[str]
) -> tuple[FieldError, ...]:
    """Map validation items using only ``loc`` and ``type`` (never ``input``/``ctx``)."""
    errors = (
        FieldError(field=safe_location(item.get("loc", ()), known), code=_safe_type(item["type"]))
        for item in items
    )
    return tuple(errors)[:MAX_FIELD_ERRORS]


def field_errors_from_validation_error(
    error: ValidationError, known: frozenset[str]
) -> tuple[FieldError, ...]:
    items = error.errors(include_input=False, include_url=False, include_context=False)
    return field_errors_from_items(items, known)


def validation_failed(field: str, code: str) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_FAILED, field_errors=(FieldError(field=field, code=code),))


def dependency_unavailable(message: str | None = None, *, retryable: bool = True) -> ApiError:
    return ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, message, retryable=retryable)


def not_found(message: str = "The requested resource was not found.") -> ApiError:
    return ApiError(ErrorCode.RESOURCE_NOT_FOUND, message)
