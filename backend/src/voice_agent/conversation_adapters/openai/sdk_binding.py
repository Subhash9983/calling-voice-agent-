"""The only module that imports the official OpenAI SDK (``openai==2.54.0``; docs/13).

Uses the SDK's async Responses API with ``stream=True`` (HTTP SSE):
``AsyncOpenAI.responses.create(**params)`` returns an ``AsyncStream`` of typed
events, iterated here and converted at this boundary:

- events become plain JSON mappings (``model_dump``);
- failures become :class:`OpenAiTransportError` with a normalized kind, the
  numeric HTTP status, and the ``x-request-id`` only. SDK exceptions are never
  logged or chained visibly (``from None``): they reference the HTTP request,
  whose headers include the ``Authorization`` value.

The client is built with the key from the WP3 credential resolver (a
``SecretStr`` unwrapped only here), the explicit official base URL (so an
ambient ``OPENAI_BASE_URL`` cannot redirect the key), ``max_retries=0`` (the
orchestrator owns retries, docs/08 §14), and a bounded HTTP timeout. The SDK
never reads ``OPENAI_API_KEY`` from the environment because the key is
always passed explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import SecretStr

from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
    ResponsesStream,
)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_REQUEST_TIMEOUT: Final = 408
HTTP_CONFLICT: Final = 409
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR: Final = 500
QUOTA_CODE: Final = "insufficient_quota"
CONTEXT_CODE: Final = "context_length_exceeded"

ClientFactory = Callable[[str, float], Any]


def default_client(api_key: str, timeout_s: float) -> Any:
    return AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL, max_retries=0, timeout=timeout_s)


def status_kind(status_code: int, code: str | None) -> OpenAiErrorKind:
    """HTTP status (+ safe error code) -> normalized kind; only 408/409/429/5xx are transient."""
    if status_code == HTTP_UNAUTHORIZED:
        return OpenAiErrorKind.AUTHENTICATION
    if status_code == HTTP_FORBIDDEN:
        return OpenAiErrorKind.PERMISSION
    if status_code == HTTP_TOO_MANY_REQUESTS:
        return OpenAiErrorKind.QUOTA if code == QUOTA_CODE else OpenAiErrorKind.RATE_LIMITED
    if status_code in (HTTP_REQUEST_TIMEOUT, HTTP_CONFLICT) or status_code >= HTTP_SERVER_ERROR:
        return OpenAiErrorKind.UNAVAILABLE
    if status_code == HTTP_BAD_REQUEST and code == CONTEXT_CODE:
        return OpenAiErrorKind.CONTEXT_TOO_LARGE
    if status_code >= HTTP_BAD_REQUEST:
        return OpenAiErrorKind.CONFIGURATION
    return OpenAiErrorKind.PROTOCOL


def normalize_error(error: BaseException) -> OpenAiTransportError:
    if isinstance(error, OpenAiTransportError):
        return error
    if isinstance(error, APITimeoutError):
        return OpenAiTransportError(OpenAiErrorKind.TOTAL_TIMEOUT)
    if isinstance(error, APIConnectionError):
        return OpenAiTransportError(OpenAiErrorKind.CONNECT_FAILED)
    if isinstance(error, APIStatusError):
        code = error.code if isinstance(error.code, str) else None
        request_id = error.request_id if isinstance(error.request_id, str) else None
        return OpenAiTransportError(
            status_kind(error.status_code, code),
            status_code=error.status_code,
            request_id=request_id,
        )
    return OpenAiTransportError(OpenAiErrorKind.PROTOCOL)


def _as_mapping(event: Any) -> Mapping[str, Any] | None:
    if isinstance(event, Mapping):
        return event
    dump = getattr(event, "model_dump", None)
    if not callable(dump):
        return None
    dumped = dump(mode="json", warnings=False)
    return dumped if isinstance(dumped, Mapping) else None


class SdkResponsesStream:
    """Wraps the SDK ``AsyncStream``; exposes only plain mappings."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.untyped_events = 0

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        iterator = self._stream.__aiter__()
        while True:
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except Exception as error:  # every SDK/HTTP failure is normalized
                raise normalize_error(error) from None
            mapping = _as_mapping(event)
            if mapping is None:
                self.untyped_events += 1
                continue
            yield mapping


class SdkResponsesConnector:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        timeout_s: float,
        client_factory: ClientFactory = default_client,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client_factory = client_factory
        self._client: Any = None
        self._closed = False

    def __repr__(self) -> str:
        return "SdkResponsesConnector()"

    def _client_instance(self) -> Any:
        if self._closed:
            raise OpenAiTransportError(OpenAiErrorKind.CONNECT_FAILED)
        if self._client is None:
            self._client = self._client_factory(self._api_key.get_secret_value(), self._timeout_s)
        return self._client

    @asynccontextmanager
    async def open(self, params: Mapping[str, Any]) -> AsyncIterator[ResponsesStream]:
        client = self._client_instance()
        try:
            stream = await client.responses.create(**params)
        except Exception as error:  # every SDK/HTTP failure is normalized
            raise normalize_error(error) from None
        try:
            yield SdkResponsesStream(stream)
        finally:
            with suppress(Exception):
                await stream.close()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            with suppress(Exception):
                await client.close()
