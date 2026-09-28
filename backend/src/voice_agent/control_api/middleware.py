"""Request context, body bound, request log, and error boundary (docs/04 §2, §19, §20).

Pure ASGI middleware (no response buffering). It assigns the backend request
and correlation IDs, rejects oversized bodies with ``413`` before or while
reading, logs one safe structured record per request, and converts any
unhandled exception into the generic ``500`` envelope without re-raising, so
no traceback or exception text reaches the browser or the server log.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from voice_agent.control_api.errors import ApiError, ErrorCode, error_envelope
from voice_agent.control_api.request_context import activate, new_request_context
from voice_agent.control_api.structured_logging import get_logger, log_event

MAX_REQUEST_BODY_BYTES = 64 * 1024
REQUEST_ID_HEADER = b"x-request-id"
_UNMATCHED_ROUTE = "<unmatched>"


class PayloadTooLargeError(HTTPException):
    """Raised from ``receive`` so FastAPI's body reader re-raises it unchanged."""

    def __init__(self) -> None:
        super().__init__(status_code=413)


async def send_error(send: Send, error: ApiError, request_id: str) -> None:
    body = json.dumps(error_envelope(error, request_id)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (REQUEST_ID_HEADER, request_id.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            text = value.decode("latin-1")
            return int(text) if text.isdigit() else -1
    return None


def _route_template(scope: Scope) -> str:
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED_ROUTE


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        context = new_request_context()
        activate(context)
        scope.setdefault("state", {})["request_id"] = context.request_id
        started = time.perf_counter()
        status = {"code": 500, "sent": False}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"], status["sent"] = message["status"], True
                headers = [h for h in message.get("headers", []) if h[0] != REQUEST_ID_HEADER]
                request_header = (REQUEST_ID_HEADER, context.request_id.encode("ascii"))
                message = {**message, "headers": [*headers, request_header]}
            await send(message)

        try:
            await self._dispatch(scope, receive, send_with_id, context.request_id)
        except Exception as exc:  # error boundary: never re-raised to the server
            log_event(get_logger(), logging.ERROR, "request.failed", error_type=type(exc).__name__)
            if not status["sent"]:
                await send_error(
                    send_with_id, ApiError(ErrorCode.INTERNAL_ERROR), context.request_id
                )
        finally:
            log_event(
                get_logger(),
                logging.INFO,
                "request.completed",
                method=scope.get("method"),
                route=_route_template(scope),
                status_code=status["code"],
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send, request_id: str) -> None:
        declared = _declared_length(scope)
        if declared is not None and (declared < 0 or declared > self.max_body_bytes):
            code = ErrorCode.PAYLOAD_TOO_LARGE if declared > 0 else ErrorCode.INVALID_REQUEST
            await send_error(send, ApiError(code), request_id)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise PayloadTooLargeError
            return message

        await self.app(scope, bounded_receive, send)
