"""Per-request correlation context (docs/04 §2, §20).

The backend generates ``request_id`` and ``correlation_id`` for every
request; browser-supplied correlation headers are never trusted. When a
request resolves an existing session, the session's own correlation ID is
bound so logs join API, worker, and event activity for that session.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    correlation_id: str
    session_id: str | None = None


_CURRENT: ContextVar[RequestContext | None] = ContextVar("control_api_request", default=None)


def new_request_context() -> RequestContext:
    return RequestContext(request_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()))


def current_context() -> RequestContext | None:
    return _CURRENT.get()


def current_request_id() -> str:
    context = _CURRENT.get()
    return context.request_id if context is not None else str(uuid.uuid4())


def current_correlation_id() -> str:
    context = _CURRENT.get()
    return context.correlation_id if context is not None else str(uuid.uuid4())


def activate(context: RequestContext) -> None:
    _CURRENT.set(context)


def bind_session(session_id: str, correlation_id: str) -> None:
    """Attach the resolved session and adopt its backend correlation ID."""
    context = _CURRENT.get()
    if context is not None:
        _CURRENT.set(replace(context, session_id=session_id, correlation_id=correlation_id))
