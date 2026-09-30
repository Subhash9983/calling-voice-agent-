"""The only module that imports the official Deepgram SDK (``deepgram-sdk==7.10.0``).

Uses the SDK's supported async live-listen connection
(``AsyncDeepgramClient().listen.v1.connect``) and its message iteration;
every SDK object and exception is converted at this boundary:

- messages become plain JSON mappings (``model_dump``);
- failures become :class:`DeepgramTransportError` with a normalized kind and
  a numeric status/close code only. SDK exceptions are never logged or
  chained visibly (``from None``): ``ApiError`` carries the request headers,
  including the ``Authorization`` value.

The API key arrives as a ``SecretStr`` from the WP3 credential resolver and
is unwrapped only when the client is built. The SDK 7.10.0 websocket
compatibility shim touches deprecated ``websockets`` names; those
``DeprecationWarning`` s are silenced locally around SDK calls only.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Final

from pydantic import SecretStr

from voice_agent.stt_adapters.deepgram.connection import (
    DeepgramConnection,
    DeepgramErrorKind,
    DeepgramTransportError,
)
from voice_agent.stt_adapters.deepgram.options import QueryValue

HTTP_UNAUTHORIZED: Final = 401
HTTP_PAYMENT_REQUIRED: Final = 402
HTTP_FORBIDDEN: Final = 403
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR: Final = 500
HTTP_CLIENT_ERROR: Final = 400
WS_POLICY_VIOLATION: Final = 1008
WS_NORMAL_CLOSURE: Final = 1000

ClientFactory = Callable[[str], Any]


@contextmanager
def _sdk_warnings() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _default_client(api_key: str) -> Any:
    with _sdk_warnings():
        from deepgram import AsyncDeepgramClient

        return AsyncDeepgramClient(api_key=api_key)


def status_kind(status_code: int | None) -> DeepgramErrorKind:
    """HTTP handshake status -> normalized kind (only 429/5xx are transient)."""
    if status_code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        return DeepgramErrorKind.AUTHENTICATION
    if status_code == HTTP_PAYMENT_REQUIRED:
        return DeepgramErrorKind.QUOTA
    if status_code == HTTP_TOO_MANY_REQUESTS:
        return DeepgramErrorKind.RATE_LIMITED
    if status_code is not None and status_code >= HTTP_SERVER_ERROR:
        return DeepgramErrorKind.UNAVAILABLE
    if status_code is not None and status_code >= HTTP_CLIENT_ERROR:
        return DeepgramErrorKind.CONFIGURATION
    return DeepgramErrorKind.CONNECT_FAILED


def close_kind(code: int | None) -> DeepgramErrorKind:
    """Abnormal websocket close -> kind; a policy close (bad audio/options) is not retried."""
    if code == WS_POLICY_VIOLATION:
        return DeepgramErrorKind.CONFIGURATION
    return DeepgramErrorKind.CONNECTION_LOST


def _close_code(error: BaseException) -> int | None:
    received = getattr(error, "rcvd", None)
    code = getattr(received, "code", None)
    return code if isinstance(code, int) else None


def _is_closed_ok(error: BaseException) -> bool:
    return type(error).__name__ == "ConnectionClosedOK" or _close_code(error) == WS_NORMAL_CLOSURE


def _message_mapping(message: Any) -> Mapping[str, Any] | None:
    if isinstance(message, Mapping):
        return message
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        return dumped if isinstance(dumped, Mapping) else None
    return None


class SdkConnection:
    """Wraps the SDK's ``AsyncV1SocketClient``; exposes only the SDK-free seam."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket
        self.binary_messages = 0

    async def _call(self, method: str, *args: Any) -> None:
        try:
            with _sdk_warnings():
                await getattr(self._socket, method)(*args)
        except Exception as error:  # every SDK/websocket failure is normalized
            raise DeepgramTransportError(
                close_kind(_close_code(error)), _close_code(error)
            ) from None

    async def send_media(self, pcm: bytes) -> None:
        await self._call("send_media", pcm)

    async def send_finalize(self) -> None:
        await self._call("send_finalize")

    async def send_keep_alive(self) -> None:
        await self._call("send_keep_alive")

    async def send_close_stream(self) -> None:
        await self._call("send_close_stream")

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        iterator = self._socket.__aiter__()
        while True:
            try:
                with _sdk_warnings():
                    message = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except Exception as error:  # every SDK/websocket failure is normalized
                if _is_closed_ok(error):
                    return
                code = _close_code(error)
                raise DeepgramTransportError(close_kind(code), code) from None
            mapping = _message_mapping(message)
            if mapping is None:
                self.binary_messages += 1
                continue
            yield mapping


class SdkDeepgramConnector:
    def __init__(self, api_key: SecretStr, *, client_factory: ClientFactory = _default_client):
        self._api_key = api_key
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return "SdkDeepgramConnector()"

    @asynccontextmanager
    async def connect(self, params: Mapping[str, QueryValue]) -> AsyncIterator[DeepgramConnection]:
        client = self._client_factory(self._api_key.get_secret_value())
        options = {k: list(v) if isinstance(v, tuple) else v for k, v in params.items()}
        opened = False
        try:
            with _sdk_warnings():
                manager = client.listen.v1.connect(**options)
                socket = await manager.__aenter__()
            opened = True
        except Exception as error:  # every SDK/websocket failure is normalized
            status = getattr(error, "status_code", None)
            code = status if isinstance(status, int) else None
            raise DeepgramTransportError(status_kind(code), code) from None
        try:
            yield SdkConnection(socket)
        finally:
            if opened:
                with _sdk_warnings():
                    await manager.__aexit__(None, None, None)
