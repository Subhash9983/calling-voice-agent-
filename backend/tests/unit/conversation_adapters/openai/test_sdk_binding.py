"""OpenAI SDK binding: event conversion and error normalization, fully offline (WP8).

Real SDK exception classes are built from local ``httpx`` objects; no request
is ever sent. The default client is constructed (no network on construction)
only to prove the explicit base URL, disabled SDK retries, and explicit key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai
import pytest
from pydantic import SecretStr

from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
)
from voice_agent.conversation_adapters.openai.sdk_binding import (
    OPENAI_BASE_URL,
    SdkResponsesConnector,
    default_client,
    normalize_error,
    status_kind,
)

pytestmark = pytest.mark.asyncio
FAKE_KEY = "sk-test-offline-not-a-real-key"
_REQUEST = httpx.Request(
    "POST", f"{OPENAI_BASE_URL}/responses", headers={"Authorization": f"Bearer {FAKE_KEY}"}
)


def _status_error(status: int, code: str | None = None) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQUEST, headers={"x-request-id": "req_123"})
    body = {"code": code, "message": "provider detail"} if code else None
    classes: dict[int, type[openai.APIStatusError]] = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
    }
    cls = classes.get(status, openai.InternalServerError)
    return cls("provider detail", response=response, body=body)


class _Event:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str, warnings: bool) -> dict[str, Any]:
        assert mode == "json"
        assert warnings is False
        return dict(self._payload)


class _Stream:
    def __init__(self, items: list[Any], fail_with: BaseException | None = None) -> None:
        self._items = items
        self._fail_with = fail_with
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for item in self._items:
            yield item
        if self._fail_with is not None:
            raise self._fail_with

    async def close(self) -> None:
        self.closed = True


@dataclass
class _Responses:
    stream: _Stream | None = None
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **params: Any) -> _Stream:
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        assert self.stream is not None
        return self.stream


@dataclass
class _Client:
    responses: _Responses
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


def _connector(client: _Client, seen: list[tuple[str, float]]) -> SdkResponsesConnector:
    def factory(api_key: str, timeout_s: float) -> _Client:
        seen.append((api_key, timeout_s))
        return client

    return SdkResponsesConnector(SecretStr(FAKE_KEY), timeout_s=45.0, client_factory=factory)


async def test_default_client_is_explicit_and_never_retries_by_itself() -> None:
    client = default_client(FAKE_KEY, 12.5)
    try:
        assert str(client.base_url).rstrip("/") == OPENAI_BASE_URL
        assert client.max_retries == 0
        assert client.api_key == FAKE_KEY
        assert client.timeout == 12.5
    finally:
        await client.close()


async def test_events_become_plain_mappings_and_the_stream_is_closed() -> None:
    stream = _Stream([_Event({"type": "response.created"}), object(), {"type": "x"}])
    client = _Client(_Responses(stream=stream))
    seen: list[tuple[str, float]] = []
    connector = _connector(client, seen)

    async with connector.open({"model": "gpt-6-luna", "stream": True}) as opened:
        events = [event async for event in opened.events()]

    assert events == [{"type": "response.created"}, {"type": "x"}]
    assert stream.closed
    assert client.responses.calls == [{"model": "gpt-6-luna", "stream": True}]
    assert seen == [(FAKE_KEY, 45.0)]
    assert repr(connector) == "SdkResponsesConnector()"
    assert FAKE_KEY not in repr(connector)


@pytest.mark.parametrize(
    ("error", "kind", "status"),
    [
        (_status_error(401), OpenAiErrorKind.AUTHENTICATION, 401),
        (_status_error(403), OpenAiErrorKind.PERMISSION, 403),
        (_status_error(429, "rate_limit_exceeded"), OpenAiErrorKind.RATE_LIMITED, 429),
        (_status_error(429, "insufficient_quota"), OpenAiErrorKind.QUOTA, 429),
        (_status_error(400, "context_length_exceeded"), OpenAiErrorKind.CONTEXT_TOO_LARGE, 400),
        (_status_error(400), OpenAiErrorKind.CONFIGURATION, 400),
        (_status_error(503), OpenAiErrorKind.UNAVAILABLE, 503),
        (openai.APIConnectionError(request=_REQUEST), OpenAiErrorKind.CONNECT_FAILED, None),
        (openai.APITimeoutError(request=_REQUEST), OpenAiErrorKind.TOTAL_TIMEOUT, None),
        (ValueError("anything else"), OpenAiErrorKind.PROTOCOL, None),
    ],
)
async def test_request_failures_are_normalized_without_chaining(
    error: BaseException, kind: OpenAiErrorKind, status: int | None
) -> None:
    connector = _connector(_Client(_Responses(error=error)), [])

    with pytest.raises(OpenAiTransportError) as raised:
        async with connector.open({}):
            pytest.fail("the stream must not open")

    assert raised.value.kind is kind
    assert raised.value.status_code == status
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert FAKE_KEY not in str(raised.value)
    if status is not None:
        assert raised.value.request_id == "req_123"


async def test_mid_stream_failure_is_normalized() -> None:
    stream = _Stream([{"type": "response.created"}], fail_with=_status_error(500))
    connector = _connector(_Client(_Responses(stream=stream)), [])

    with pytest.raises(OpenAiTransportError) as raised:
        async with connector.open({}) as opened:
            _ = [event async for event in opened.events()]

    assert raised.value.kind is OpenAiErrorKind.UNAVAILABLE
    assert stream.closed


async def test_aclose_is_idempotent_and_blocks_later_requests() -> None:
    client = _Client(_Responses(stream=_Stream([])))
    connector = _connector(client, [])
    async with connector.open({}):
        pass

    await connector.aclose()
    await connector.aclose()

    assert client.closed
    with pytest.raises(OpenAiTransportError):
        async with connector.open({}):
            pass


async def test_status_kind_table_and_passthrough() -> None:
    assert status_kind(408, None) is OpenAiErrorKind.UNAVAILABLE
    assert status_kind(409, None) is OpenAiErrorKind.UNAVAILABLE
    assert status_kind(404, None) is OpenAiErrorKind.CONFIGURATION
    assert status_kind(302, None) is OpenAiErrorKind.PROTOCOL
    already = OpenAiTransportError(OpenAiErrorKind.SAFETY)
    assert normalize_error(already) is already
