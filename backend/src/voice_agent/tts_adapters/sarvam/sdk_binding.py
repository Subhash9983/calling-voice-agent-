"""The only module that imports the official Sarvam SDK (``sarvamai==0.1.34``; docs/13 §8).

Uses the SDK's async streaming TTS WebSocket
(``AsyncSarvamAI.text_to_speech_streaming.connect(model="bulbul:v3",
send_completion_event="true")``) and its public socket methods
(``configure``/``convert``/``flush``/``ping``/``recv``). Everything is
converted at this boundary:

- messages become frozen seam messages; base64 audio is decoded here;
  provider error text and details are dropped (only the numeric code and the
  request ID are kept);
- failures become :class:`SarvamTransportError` with a normalized kind and
  a numeric status only. SDK exceptions are never logged or chained visibly
  (``from None``): the SDK's handshake ``ApiError`` carries the request
  headers, which include the ``Api-Subscription-Key``.

The key arrives as a ``SecretStr`` from the WP3 credential resolver and is
unwrapped only when the client is built; it is always passed explicitly, so
the SDK's ``SARVAM_API_KEY`` environment default is never used. Bulbul v3
ignores pitch/loudness; the SDK ``configure`` helper sends its neutral
defaults for them. The SDK's websocket compatibility shim touches deprecated
``websockets`` names; those ``DeprecationWarning`` s are silenced locally
around SDK calls only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final

from pydantic import SecretStr

from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    ErrorMessage,
    FinalMessage,
    SarvamErrorKind,
    SarvamMessage,
    SarvamTransportError,
    StreamSettings,
    code_kind,
)
from voice_agent.tts_adapters.sarvam.options import SARVAM_MODEL

OPEN_TIMEOUT_S: Final = 10.0
WS_NORMAL_CLOSURE: Final = 1000
COMPLETION_EVENT: Final = "final"

ClientFactory = Callable[[str], Any]


@contextmanager
def _sdk_warnings() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _default_client(api_key: str) -> Any:
    with _sdk_warnings():
        from sarvamai import AsyncSarvamAI

        return AsyncSarvamAI(api_subscription_key=api_key)


def _close_code(error: BaseException) -> int | None:
    received = getattr(error, "rcvd", None)
    code = getattr(received, "code", None)
    return code if isinstance(code, int) else None


def _is_closed_ok(error: BaseException) -> bool:
    return type(error).__name__ == "ConnectionClosedOK" or _close_code(error) == WS_NORMAL_CLOSURE


def _is_closed(error: BaseException) -> bool:
    return type(error).__name__.startswith("ConnectionClosed")


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def to_message(message: Any) -> SarvamMessage | None:
    """SDK response model -> seam message; ``None`` for anything unrecognized."""
    kind = getattr(message, "type", None)
    data = getattr(message, "data", None)
    if kind == "audio" and data is not None:
        encoded = getattr(data, "audio", None)
        if not isinstance(encoded, str):
            raise SarvamTransportError(SarvamErrorKind.CORRUPT_AUDIO)
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SarvamTransportError(SarvamErrorKind.CORRUPT_AUDIO) from None
        content_type = getattr(data, "content_type", None)
        return AudioMessage(
            pcm=pcm,
            content_type=content_type if isinstance(content_type, str) else "",
            request_id=_optional_str(getattr(data, "request_id", None)),
        )
    if kind == "event" and getattr(data, "event_type", None) == COMPLETION_EVENT:
        return FinalMessage()
    if kind == "error" and data is not None:
        code = getattr(data, "code", None)
        return ErrorMessage(
            code=code if isinstance(code, int) else None,
            request_id=_optional_str(getattr(data, "request_id", None)),
        )
    return None


class SdkSarvamStream:
    """Wraps the SDK ``AsyncTextToSpeechStreamingSocketClient``; exposes only the seam."""

    def __init__(self, manager: Any, socket: Any) -> None:
        self._manager = manager
        self._socket = socket
        self._closed = False
        self.unrecognized_messages = 0

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        if self._closed:
            raise SarvamTransportError(SarvamErrorKind.CONNECTION_LOST)
        try:
            with _sdk_warnings():
                await getattr(self._socket, method)(*args, **kwargs)
        except Exception as error:  # every SDK/websocket failure is normalized
            raise SarvamTransportError(
                SarvamErrorKind.CONNECTION_LOST, _close_code(error)
            ) from None

    async def configure(self, settings: StreamSettings) -> None:
        await self._call(
            "configure",
            target_language_code=settings.language_code,
            speaker=settings.speaker,
            pace=settings.pace,
            speech_sample_rate=settings.sample_rate_hz,
            output_audio_codec=settings.output_audio_codec,
            min_buffer_size=settings.min_buffer_size,
            max_chunk_length=settings.max_chunk_length,
        )

    async def send_text(self, text: str) -> None:
        await self._call("convert", text)

    async def flush(self) -> None:
        await self._call("flush")

    async def ping(self) -> None:
        await self._call("ping")

    async def messages(self) -> AsyncIterator[SarvamMessage]:
        while not self._closed:
            try:
                with _sdk_warnings():
                    raw = await self._socket.recv()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # every SDK/websocket failure is normalized
                if _is_closed_ok(error):
                    return
                if _is_closed(error):
                    raise SarvamTransportError(
                        SarvamErrorKind.CONNECTION_LOST, _close_code(error)
                    ) from None
                self.unrecognized_messages += 1  # an unparseable provider message
                continue
            message = to_message(raw)
            if message is None:
                self.unrecognized_messages += 1
                continue
            yield message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with _sdk_warnings():
                await self._manager.__aexit__(None, None, None)
        except Exception:  # closing never raises past the seam
            return


class SdkSarvamConnector:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        client_factory: ClientFactory = _default_client,
        open_timeout_s: float = OPEN_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._client_factory = client_factory
        self._open_timeout_s = open_timeout_s
        self._client: Any = None
        self._closed = False

    def __repr__(self) -> str:
        return "SdkSarvamConnector()"

    def _client_instance(self) -> Any:
        if self._closed:
            raise SarvamTransportError(SarvamErrorKind.CONNECT_FAILED)
        if self._client is None:
            self._client = self._client_factory(self._api_key.get_secret_value())
        return self._client

    async def open(self) -> SdkSarvamStream:
        client = self._client_instance()
        try:
            with _sdk_warnings():
                manager = client.text_to_speech_streaming.connect(
                    model=SARVAM_MODEL, send_completion_event="true"
                )
                async with asyncio.timeout(self._open_timeout_s):
                    socket = await manager.__aenter__()
        except Exception as error:  # every SDK/websocket failure is normalized
            status = getattr(error, "status_code", None)
            if isinstance(status, int):
                raise SarvamTransportError(code_kind(status), status) from None
            raise SarvamTransportError(SarvamErrorKind.CONNECT_FAILED) from None
        return SdkSarvamStream(manager, socket)

    async def aclose(self) -> None:
        self._closed = True
        self._client = None
