"""SDK binding: normalization at the Deepgram SDK boundary (offline; no socket is opened)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from voice_agent.stt_adapters.deepgram.connection import DeepgramErrorKind, DeepgramTransportError
from voice_agent.stt_adapters.deepgram.sdk_binding import (
    SdkConnection,
    SdkDeepgramConnector,
    _default_client,
    close_kind,
    status_kind,
)

pytestmark = pytest.mark.asyncio
FAKE_KEY = "unit-test-not-a-real-key-000"


class ConnectionClosedError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"closed {code} with provider detail")
        self.rcvd = SimpleNamespace(code=code)


class ConnectionClosedOK(Exception):  # noqa: N818 - mirrors the websockets name
    pass


class ProviderApiError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("Authorization: Token secret-header-value")
        self.status_code = status_code
        self.headers = {"Authorization": "Token secret-header-value"}


class Model:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return dict(self._data)


class FakeSocket:
    def __init__(self, items: list[Any], *, fail_send: Exception | None = None) -> None:
        self._items = items
        self._fail_send = fail_send
        self.sent: list[tuple[str, tuple[Any, ...]]] = []

    async def __aiter__(self) -> AsyncIterator[Any]:
        for item in self._items:
            if isinstance(item, Exception):
                raise item
            yield item

    async def _record(self, name: str, *args: Any) -> None:
        if self._fail_send is not None:
            raise self._fail_send
        self.sent.append((name, args))

    async def send_media(self, pcm: bytes) -> None:
        await self._record("media", pcm)

    async def send_finalize(self) -> None:
        await self._record("finalize")

    async def send_keep_alive(self) -> None:
        await self._record("keepalive")

    async def send_close_stream(self) -> None:
        await self._record("close_stream")


class FakeClient:
    def __init__(self, socket: FakeSocket | None, error: Exception | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self.exited = False
        self._socket, self._error = socket, error
        self.listen = SimpleNamespace(v1=SimpleNamespace(connect=self._connect))

    @asynccontextmanager
    async def _connect(self, **kwargs: Any) -> AsyncIterator[FakeSocket]:
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        assert self._socket is not None
        try:
            yield self._socket
        finally:
            self.exited = True


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, DeepgramErrorKind.AUTHENTICATION),
        (403, DeepgramErrorKind.AUTHENTICATION),
        (402, DeepgramErrorKind.QUOTA),
        (429, DeepgramErrorKind.RATE_LIMITED),
        (503, DeepgramErrorKind.UNAVAILABLE),
        (400, DeepgramErrorKind.CONFIGURATION),
        (None, DeepgramErrorKind.CONNECT_FAILED),
    ],
)
async def test_handshake_status_mapping(status: int | None, kind: DeepgramErrorKind) -> None:
    assert status_kind(status) is kind


async def test_close_code_mapping() -> None:
    assert close_kind(1008) is DeepgramErrorKind.CONFIGURATION
    assert close_kind(1011) is DeepgramErrorKind.CONNECTION_LOST
    assert close_kind(None) is DeepgramErrorKind.CONNECTION_LOST


async def test_messages_become_plain_mappings_and_binary_frames_are_skipped() -> None:
    socket = FakeSocket([Model({"type": "Results"}), b"\x00", {"type": "Metadata"}])

    connection = SdkConnection(socket)
    received = [dict(m) async for m in connection.messages()]

    assert received == [{"type": "Results"}, {"type": "Metadata"}]
    assert connection.binary_messages == 1


async def test_abnormal_close_is_normalized_without_provider_detail() -> None:
    connection = SdkConnection(
        FakeSocket([Model({"type": "Results"}), ConnectionClosedError(1011)])
    )

    with pytest.raises(DeepgramTransportError) as raised:
        _ = [m async for m in connection.messages()]

    assert raised.value.kind is DeepgramErrorKind.CONNECTION_LOST
    assert raised.value.status_code == 1011
    assert str(raised.value) == "connection_lost"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


async def test_normal_close_ends_the_message_stream() -> None:
    connection = SdkConnection(FakeSocket([ConnectionClosedOK()]))

    assert [m async for m in connection.messages()] == []


async def test_control_messages_and_send_failures() -> None:
    socket = FakeSocket([])
    connection = SdkConnection(socket)

    await connection.send_media(b"\x01\x02")
    await connection.send_finalize()
    await connection.send_keep_alive()
    await connection.send_close_stream()

    assert [name for name, _ in socket.sent] == ["media", "finalize", "keepalive", "close_stream"]
    failing = SdkConnection(FakeSocket([], fail_send=ConnectionClosedError(1008)))
    with pytest.raises(DeepgramTransportError) as raised:
        await failing.send_media(b"\x00\x00")
    assert raised.value.kind is DeepgramErrorKind.CONFIGURATION


async def test_connector_passes_options_and_closes_the_socket() -> None:
    client = FakeClient(FakeSocket([]))
    keys: list[str] = []

    def factory(key: str) -> FakeClient:
        keys.append(key)
        return client

    connector = SdkDeepgramConnector(SecretStr(FAKE_KEY), client_factory=factory)
    async with connector.connect({"model": "nova-3", "keyterm": ("A", "B")}) as connection:
        assert isinstance(connection, SdkConnection)

    assert keys == [FAKE_KEY]
    assert client.kwargs == {"model": "nova-3", "keyterm": ["A", "B"]}
    assert client.exited
    assert FAKE_KEY not in repr(connector)


async def test_handshake_failure_never_chains_the_sdk_error_with_headers() -> None:
    client = FakeClient(None, error=ProviderApiError(401))
    connector = SdkDeepgramConnector(SecretStr(FAKE_KEY), client_factory=lambda _key: client)

    with pytest.raises(DeepgramTransportError) as raised:
        async with connector.connect({"model": "nova-3"}):
            pass

    assert raised.value.kind is DeepgramErrorKind.AUTHENTICATION
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "secret-header-value" not in str(raised.value)


async def test_the_official_sdk_client_builds_offline_without_warnings() -> None:
    client = _default_client(FAKE_KEY)

    assert type(client).__name__ == "AsyncDeepgramClient"
