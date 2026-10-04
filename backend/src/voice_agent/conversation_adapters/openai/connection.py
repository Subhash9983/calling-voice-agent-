"""SDK-free seam between the OpenAI adapter and the SDK binding (docs/03 §4).

The adapter and its tests see only plain JSON mappings and a normalized
transport error carrying a kind, a numeric HTTP status, and the provider
request ID (safe evidence). No SDK object, header, body, prompt, or
credential crosses this seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any, Protocol


class OpenAiErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CONFIGURATION = "configuration"
    CONTEXT_TOO_LARGE = "context_too_large"
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"
    UNAVAILABLE = "unavailable"
    CONNECT_FAILED = "connect_failed"
    CONNECTION_LOST = "connection_lost"
    FIRST_TOKEN_TIMEOUT = "first_token_timeout"  # noqa: S105 - error kind name
    TOTAL_TIMEOUT = "total_timeout"
    SAFETY = "safety"
    TOOL_ACTIVITY = "tool_activity"
    PROTOCOL = "protocol"


class OpenAiTransportError(Exception):
    """A normalized SDK/stream failure; never carries provider text."""

    def __init__(
        self,
        kind: OpenAiErrorKind,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.status_code = status_code
        self.request_id = request_id


class ResponsesStream(Protocol):
    def events(self) -> AsyncIterator[Mapping[str, Any]]:
        """Ordered server-sent events as plain mappings; raises ``OpenAiTransportError``."""
        ...


class ResponsesConnector(Protocol):
    def open(self, params: Mapping[str, Any]) -> AbstractAsyncContextManager[ResponsesStream]:
        """Start one streamed Responses request; closing the context closes the stream."""
        ...

    async def aclose(self) -> None:
        """Release the HTTP client idempotently."""
        ...
