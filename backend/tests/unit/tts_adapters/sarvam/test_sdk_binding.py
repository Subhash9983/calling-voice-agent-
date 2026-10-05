"""Sarvam SDK binding: normalization at the SDK boundary (offline; fake socket, real SDK models)."""

from __future__ import annotations

import asyncio
import base64
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr

from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    ErrorMessage,
    FinalMessage,
    SarvamErrorKind,
    SarvamTransportError,
    StreamSettings,
)
from voice_agent.tts_adapters.sarvam.sdk_binding import (
    SdkSarvamConnector,
    SdkSarvamStream,
    _default_client,
    to_message,
)

CANARY = "sarvam-canary-" + "Zz9Yy8Xx7"  # synthetic, never a real key
SETTINGS = StreamSettings("hi-IN", "priya", 1.0, 24_000, "linear16", 50, 150)


@contextmanager
def _quiet() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _sdk_types() -> Any:
    with _quiet():
        from sarvamai import types

        return types


class ConnectionClosedOK(Exception):  # noqa: N818 - mirrors the websockets class name
    pass


class ConnectionClosedError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("closed")
        self.rcvd = type("Close", (), {"code": code})()


class HandshakeError(Exception):
    """Stands in for the SDK ``ApiError`` (which carries the request headers)."""

    def __init__(self, status_code: int) -> None:
        super().__init__({"Api-Subscription-Key": CANARY})
        self.status_code = status_code


@dataclass
class FakeSocket:
    received: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    fail: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        if name not in {"configure", "convert", "flush", "ping"}:
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> None:
            if self.fail is not None:
                raise self.fail
            self.calls.append((name, args, kwargs))

        return call

    async def recv(self) -> Any:
        if not self.received:
            raise ConnectionClosedOK()
        item = self.received.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class FakeManager:
    socket: FakeSocket
    enter_error: Exception | None = None
    exits: int = 0
    exit_error: Exception | None = None
    hang: bool = False

    async def __aenter__(self) -> FakeSocket:
        if self.hang:
            await asyncio.sleep(3600)
        if self.enter_error is not None:
            raise self.enter_error
        return self.socket

    async def __aexit__(self, *_exc: object) -> None:
        self.exits += 1
        if self.exit_error is not None:
            raise self.exit_error


@dataclass
class FakeClient:
    manager: FakeManager
    connects: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text_to_speech_streaming(self) -> FakeClient:
        return self

    def connect(self, **kwargs: Any) -> FakeManager:
        self.connects.append(kwargs)
        return self.manager


def _connector(
    manager: FakeManager, keys: list[str] | None = None
) -> tuple[SdkSarvamConnector, FakeClient]:
    client = FakeClient(manager)

    def factory(key: str) -> FakeClient:
        (keys if keys is not None else []).append(key)
        return client

    return SdkSarvamConnector(
        SecretStr(CANARY), client_factory=factory, open_timeout_s=0.05
    ), client


@pytest.mark.asyncio
async def test_open_requests_bulbul_v3_with_completion_events_and_the_explicit_key() -> None:
    keys: list[str] = []
    connector, client = _connector(FakeManager(FakeSocket()), keys)

    stream = await connector.open()

    assert isinstance(stream, SdkSarvamStream)
    assert client.connects == [{"model": "bulbul:v3", "send_completion_event": "true"}]
    assert keys == [CANARY]
    assert CANARY not in repr(connector)


@pytest.mark.parametrize(
    ("error", "kind", "status"),
    [
        (HandshakeError(401), SarvamErrorKind.AUTHENTICATION, 401),
        (HandshakeError(429), SarvamErrorKind.RATE_LIMITED, 429),
        (HandshakeError(503), SarvamErrorKind.UNAVAILABLE, 503),
        (OSError("dns"), SarvamErrorKind.CONNECT_FAILED, None),
    ],
)
@pytest.mark.asyncio
async def test_handshake_failures_are_normalized_without_chaining(
    error: Exception, kind: SarvamErrorKind, status: int | None
) -> None:
    connector, _ = _connector(FakeManager(FakeSocket(), enter_error=error))

    with pytest.raises(SarvamTransportError) as raised:
        await connector.open()

    assert (raised.value.kind, raised.value.status_code) == (kind, status)
    assert raised.value.__suppress_context__
    assert CANARY not in str(raised.value)


@pytest.mark.asyncio
async def test_open_is_bounded_and_refused_after_close() -> None:
    connector, _ = _connector(FakeManager(FakeSocket(), hang=True))

    with pytest.raises(SarvamTransportError) as raised:
        await connector.open()
    await connector.aclose()
    with pytest.raises(SarvamTransportError) as closed:
        await connector.open()

    assert raised.value.kind is SarvamErrorKind.CONNECT_FAILED
    assert closed.value.kind is SarvamErrorKind.CONNECT_FAILED


@pytest.mark.asyncio
async def test_stream_methods_map_to_public_socket_calls() -> None:
    socket = FakeSocket()
    stream = SdkSarvamStream(FakeManager(socket), socket)

    await stream.configure(SETTINGS)
    await stream.send_text("नमस्ते")
    await stream.flush()
    await stream.ping()

    names = [name for name, _, _ in socket.calls]
    assert names == ["configure", "convert", "flush", "ping"]
    configure = socket.calls[0][2]
    assert configure["target_language_code"] == "hi-IN"
    assert configure["speaker"] == "priya"
    assert configure["speech_sample_rate"] == 24_000
    assert configure["output_audio_codec"] == "linear16"
    assert "pitch" not in configure
    assert "loudness" not in configure
    assert socket.calls[1][1] == ("नमस्ते",)


@pytest.mark.asyncio
async def test_socket_failures_become_connection_lost() -> None:
    socket = FakeSocket(fail=ConnectionClosedError(1011))
    stream = SdkSarvamStream(FakeManager(socket), socket)

    with pytest.raises(SarvamTransportError) as raised:
        await stream.send_text("x")
    await stream.close()
    with pytest.raises(SarvamTransportError):
        await stream.flush()

    assert (raised.value.kind, raised.value.status_code) == (SarvamErrorKind.CONNECTION_LOST, 1011)


def test_real_sdk_models_map_to_seam_messages() -> None:
    types = _sdk_types()
    audio = types.AudioOutput(
        data=types.AudioOutputData(
            content_type="audio/pcm", audio=base64.b64encode(b"\x01\x02").decode(), request_id="r1"
        )
    )
    final = types.EventResponse(data=types.EventResponseData(event_type="final"))
    err = types.ErrorResponse(
        data=types.ErrorResponseData(message="secret detail", code=429, request_id="r2")
    )

    assert to_message(audio) == AudioMessage(b"\x01\x02", "audio/pcm", "r1")
    assert to_message(final) == FinalMessage()
    assert to_message(err) == ErrorMessage(code=429, request_id="r2")
    assert to_message(object()) is None


def test_undecodable_audio_is_corrupt() -> None:
    types = _sdk_types()
    bad = types.AudioOutput(data=types.AudioOutputData(content_type="audio/pcm", audio="***"))
    missing = type("M", (), {"type": "audio", "data": type("D", (), {"audio": None})()})()

    for message in (bad, missing):
        with pytest.raises(SarvamTransportError) as raised:
            to_message(message)
        assert raised.value.kind is SarvamErrorKind.CORRUPT_AUDIO


@pytest.mark.asyncio
async def test_messages_skip_unrecognized_and_end_on_normal_close() -> None:
    types = _sdk_types()
    final = types.EventResponse(data=types.EventResponseData(event_type="final"))
    socket = FakeSocket(received=[ValueError("unparseable"), object(), final])
    stream = SdkSarvamStream(FakeManager(socket), socket)

    received = [message async for message in stream.messages()]

    assert received == [FinalMessage()]
    assert stream.unrecognized_messages == 2


@pytest.mark.asyncio
async def test_messages_raise_on_abnormal_close() -> None:
    socket = FakeSocket(received=[ConnectionClosedError(1011)])
    stream = SdkSarvamStream(FakeManager(socket), socket)

    with pytest.raises(SarvamTransportError) as raised:
        _ = [message async for message in stream.messages()]

    assert raised.value.kind is SarvamErrorKind.CONNECTION_LOST


@pytest.mark.asyncio
async def test_close_is_idempotent_and_never_raises() -> None:
    socket = FakeSocket()
    manager = FakeManager(socket, exit_error=RuntimeError("boom"))
    stream = SdkSarvamStream(manager, socket)

    await stream.close()
    await stream.close()

    assert manager.exits == 1
    assert [m async for m in stream.messages()] == []


def test_default_client_is_the_official_async_client_without_network() -> None:
    client = _default_client(CANARY)

    assert type(client).__name__ == "AsyncSarvamAI"
    with _quiet():  # the SDK resolves the streaming client lazily (deprecated shim)
        assert callable(client.text_to_speech_streaming.connect)
