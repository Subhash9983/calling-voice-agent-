"""SDK-free seam between the Sarvam TTS adapter and its SDK binding (docs/03 §4).

The adapter and its tests see only these frozen messages, the stream
settings, and a normalized transport error carrying a kind and a numeric
status/code. No SDK object, header, provider text, or credential crosses
this seam; base64 audio is already decoded to bytes by the binding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_PAYMENT_REQUIRED: Final = 402
HTTP_FORBIDDEN: Final = 403
HTTP_UNPROCESSABLE: Final = 422
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR: Final = 500


class SarvamErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    RATE_LIMITED = "rate_limited"
    CONFIGURATION = "configuration"
    UNAVAILABLE = "unavailable"
    CONNECT_FAILED = "connect_failed"
    CONNECTION_LOST = "connection_lost"
    FIRST_AUDIO_TIMEOUT = "first_audio_timeout"
    TOTAL_TIMEOUT = "total_timeout"
    CORRUPT_AUDIO = "corrupt_audio"
    PROTOCOL = "protocol"


class SarvamTransportError(Exception):
    """A normalized SDK/websocket failure; never carries provider text."""

    def __init__(self, kind: SarvamErrorKind, status_code: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.status_code = status_code


def code_kind(code: int | None) -> SarvamErrorKind:
    """Handshake HTTP status or in-stream error code -> kind (429/5xx are transient)."""
    if code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        return SarvamErrorKind.AUTHENTICATION
    if code == HTTP_PAYMENT_REQUIRED:
        return SarvamErrorKind.QUOTA
    if code == HTTP_TOO_MANY_REQUESTS:
        return SarvamErrorKind.RATE_LIMITED
    if code is not None and code >= HTTP_SERVER_ERROR:
        return SarvamErrorKind.UNAVAILABLE
    if code is not None and code >= HTTP_BAD_REQUEST:
        return SarvamErrorKind.CONFIGURATION
    return SarvamErrorKind.PROTOCOL


@dataclass(frozen=True, slots=True)
class StreamSettings:
    """The provider ``config`` message for one stream (mapped by the binding)."""

    language_code: str
    speaker: str
    pace: float
    sample_rate_hz: int
    output_audio_codec: str
    min_buffer_size: int
    max_chunk_length: int


@dataclass(frozen=True, slots=True)
class AudioMessage:
    pcm: bytes
    content_type: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinalMessage:
    """``send_completion_event``: the last audio chunk of the flushed text was sent."""


@dataclass(frozen=True, slots=True)
class ErrorMessage:
    code: int | None = None
    request_id: str | None = None


SarvamMessage = AudioMessage | FinalMessage | ErrorMessage


class SarvamStream(Protocol):
    async def configure(self, settings: StreamSettings) -> None: ...

    async def send_text(self, text: str) -> None: ...

    async def flush(self) -> None: ...

    async def ping(self) -> None: ...

    def messages(self) -> AsyncIterator[SarvamMessage]:
        """One ordered iterator per stream; raises ``SarvamTransportError``."""
        ...

    async def close(self) -> None:
        """Close idempotently; any pending receive ends."""
        ...


class SarvamConnector(Protocol):
    async def open(self) -> SarvamStream:
        """Open one authenticated Bulbul v3 streaming connection."""
        ...

    async def aclose(self) -> None:
        """Release client resources idempotently."""
        ...
