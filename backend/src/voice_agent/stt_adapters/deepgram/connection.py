"""SDK-free seam between the Deepgram adapter and the official SDK binding.

The binding (:mod:`sdk_binding`) implements :class:`DeepgramConnector`;
tests use a scripted fake. Messages cross this seam as plain JSON mappings
and failures as :class:`DeepgramTransportError` carrying only a normalized
kind and an optional numeric status/close code, never a provider message,
header, URL, or credential.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any, Protocol

from voice_agent.stt_adapters.deepgram.options import QueryValue


class DeepgramErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    QUOTA = "quota"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    CONNECT_FAILED = "connect_failed"
    CONNECTION_LOST = "connection_lost"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"


class DeepgramTransportError(Exception):
    """Normalized Deepgram transport failure; ``str()`` is the kind only."""

    def __init__(self, kind: DeepgramErrorKind, status_code: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.status_code = status_code


class DeepgramConnection(Protocol):
    async def send_media(self, pcm: bytes) -> None: ...

    async def send_finalize(self) -> None: ...

    async def send_keep_alive(self) -> None: ...

    async def send_close_stream(self) -> None: ...

    def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        """Provider messages in order; ends on a normal close, raises on loss."""
        ...


class DeepgramConnector(Protocol):
    def connect(
        self, params: Mapping[str, QueryValue]
    ) -> AbstractAsyncContextManager[DeepgramConnection]:
        """Open one live-listen stream; the context closes the socket."""
        ...
