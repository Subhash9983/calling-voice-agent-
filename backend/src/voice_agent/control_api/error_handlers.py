"""FastAPI exception mapping onto the approved error envelope (docs/04 §19).

Validation failures expose only a sanitized location and Pydantic error
type; known request field names (and known restricted names such as
``model``) are shown, any other key is ``<unrecognized>``. Framework HTTP
errors map onto the allowlisted codes; nothing else reaches the browser.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from voice_agent.control_api.errors import (
    ApiError,
    ErrorCode,
    error_envelope,
    field_errors_from_items,
)
from voice_agent.control_api.request_context import current_request_id
from voice_agent.security.overrides import RESTRICTED_BROWSER_FIELDS

PATH_PARAMETERS: frozenset[str] = frozenset(
    {"session_id", "turn_id", "agent_config_id", "consent_chain_id"}
)
_HTTP_CODES: Mapping[int, ErrorCode] = types.MappingProxyType(
    {
        400: ErrorCode.INVALID_REQUEST,
        403: ErrorCode.ACCESS_FORBIDDEN,
        404: ErrorCode.RESOURCE_NOT_FOUND,
        # Unsupported methods have no dedicated code in the §19 allowlist.
        405: ErrorCode.INVALID_REQUEST,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        415: ErrorCode.INVALID_REQUEST,
    }
)


def _model_types(annotation: object) -> Iterable[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from _model_types(argument)


def model_field_names(models: Iterable[type[BaseModel]]) -> frozenset[str]:
    """All field names reachable from ``models`` (recursing into nested models)."""
    names: set[str] = set()
    pending, seen = list(models), set[type[BaseModel]]()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            names.add(name)
            pending.extend(_model_types(info.annotation))
    return frozenset(names)


def _respond(error: ApiError) -> JSONResponse:
    return JSONResponse(error_envelope(error, current_request_id()), status_code=error.status_code)


def install_exception_handlers(app: FastAPI, request_models: Iterable[type[BaseModel]]) -> None:
    known = model_field_names(request_models) | RESTRICTED_BROWSER_FIELDS | PATH_PARAMETERS

    async def on_api_error(_request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, ApiError):  # pragma: no cover - registration guarantees it
            return _respond(ApiError(ErrorCode.INTERNAL_ERROR))
        return _respond(exc)

    async def on_validation(_request: Request, exc: Exception) -> JSONResponse:
        items = exc.errors() if isinstance(exc, RequestValidationError) else ()
        fields = field_errors_from_items(items, known)
        return _respond(ApiError(ErrorCode.VALIDATION_FAILED, field_errors=fields))

    async def on_http(_request: Request, exc: Exception) -> JSONResponse:
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        return _respond(ApiError(_HTTP_CODES.get(status, ErrorCode.INTERNAL_ERROR)))

    app.add_exception_handler(ApiError, on_api_error)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
