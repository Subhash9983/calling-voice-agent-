"""Suite-wide gating for tests that touch real R&D services.

``atlas``-marked tests (the R&D MongoDB Atlas database), ``livekit``-marked
tests (LiveKit Cloud, metered), ``deepgram``-marked tests (Deepgram live
STT, metered), ``openai``-marked tests (OpenAI GPT-6 Luna, metered), and
``sarvam``-marked tests (Sarvam Bulbul v3 live TTS, metered in INR) are
skipped unless their marker is selected explicitly with ``-m`` *and*
``VOICE_AGENT_SECRETS_FILE`` is present in the process environment, so a
default ``pytest`` run is always offline.

``openai`` tests additionally require ``VOICE_AGENT_OPENAI_LIVE_APPROVED=1``:
the WP8 live budget is still PENDING in docs/15 §2.4, and an ambient
``OPENAI_API_KEY`` user variable must never be enough to spend money.

``sarvam`` tests additionally require ``VOICE_AGENT_SARVAM_LIVE_APPROVED=1``
(the WP9 INR 50.00 live budget recorded in docs/15 §2.5, approved
2026-10-05); the secrets file alone is never enough to spend money.

Every test that is not ``openai``-marked runs behind a guard that fails any
HTTP request to an ``openai.com`` host, and every test that is not
``sarvam``-marked fails any HTTP request *or DNS resolution* (the SDK's
WebSocket does not use ``httpx``) for a ``sarvam.ai`` host, so offline runs
provably make no OpenAI or Sarvam call.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

SECRETS_FILE_VARIABLE = "VOICE_AGENT_SECRETS_FILE"
OPENAI_APPROVAL_VARIABLE = "VOICE_AGENT_OPENAI_LIVE_APPROVED"
SARVAM_APPROVAL_VARIABLE = "VOICE_AGENT_SARVAM_LIVE_APPROVED"
APPROVAL_VARIABLES = {"openai": OPENAI_APPROVAL_VARIABLE, "sarvam": SARVAM_APPROVAL_VARIABLE}
REAL_SERVICE_MARKERS = ("atlas", "livekit", "deepgram", "openai", "sarvam")
BLOCKED_HOST_SUFFIX = "openai.com"
SARVAM_HOST_SUFFIX = "sarvam.ai"
# pymongo 4.18.1's background server monitor can leave a connecting socket for
# the GC when a client closes mid-connect on Windows (traced to
# ``pymongo/asynchronous/monitor.py`` -> ``pool.py``). That is driver-internal,
# so only socket ResourceWarnings are ignored, and only for real-Atlas tests.
ATLAS_SOCKET_WARNING_FILTERS = (
    "ignore:unclosed <socket.socket:ResourceWarning",
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning",
)


def _selected(config: pytest.Config, marker: str) -> bool:
    expression = str(config.getoption("markexpr") or "")
    return marker in expression and f"not {marker}" not in expression


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    configured = bool(os.environ.get(SECRETS_FILE_VARIABLE))
    for item in items:
        if item.get_closest_marker("atlas") is not None:
            for spec in ATLAS_SOCKET_WARNING_FILTERS:
                item.add_marker(pytest.mark.filterwarnings(spec))
        for marker in REAL_SERVICE_MARKERS:
            # A real marker only: ``item.keywords`` also holds path parts such
            # as the ``livekit`` test package name.
            if item.get_closest_marker(marker) is None:
                continue
            if not _selected(config, marker):
                item.add_marker(
                    pytest.mark.skip(reason=f"{marker} tests run only with -m {marker}")
                )
            elif not configured:
                item.add_marker(pytest.mark.skip(reason=f"{SECRETS_FILE_VARIABLE} is not set"))
            elif marker in APPROVAL_VARIABLES and os.environ.get(APPROVAL_VARIABLES[marker]) != "1":
                item.add_marker(
                    pytest.mark.skip(reason=f"{APPROVAL_VARIABLES[marker]}=1 is required")
                )


class OpenAiEgressBlockedError(RuntimeError):
    """An offline test attempted an HTTP request to an OpenAI host."""


def _matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _is_openai_host(request: httpx.Request) -> bool:
    return _matches(request.url.host or "", BLOCKED_HOST_SUFFIX)


class SarvamEgressBlockedError(OSError):
    """An offline test attempted to reach a Sarvam host (HTTP or WebSocket)."""


@pytest.fixture(autouse=True)
def sarvam_egress_attempts(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str]]:
    """Fail (and record) any resolution of / request to a Sarvam host outside ``sarvam`` tests.

    Name resolution is the choke point shared by ``httpx`` (REST) and the
    ``websockets`` client the Sarvam SDK uses for streaming TTS.
    """
    attempts: list[str] = []
    if request.node.get_closest_marker("sarvam") is not None:
        yield attempts
        return
    resolve = socket.getaddrinfo

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if _matches(name.rstrip(".").lower(), SARVAM_HOST_SUFFIX):
            attempts.append(name)
            raise SarvamEgressBlockedError(name)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield attempts


@pytest.fixture(autouse=True)
def openai_egress_attempts(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str]]:
    """Fail (and record) any request to an OpenAI host outside ``openai`` tests."""
    attempts: list[str] = []
    if request.node.get_closest_marker("openai") is not None:
        yield attempts
        return
    sync_send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_sync(self: httpx.HTTPTransport, outgoing: httpx.Request) -> Any:
        if _is_openai_host(outgoing):
            attempts.append(outgoing.url.host)
            raise OpenAiEgressBlockedError(outgoing.url.host)
        return sync_send(self, outgoing)

    async def guarded_async(self: httpx.AsyncHTTPTransport, outgoing: httpx.Request) -> Any:
        if _is_openai_host(outgoing):
            attempts.append(outgoing.url.host)
            raise OpenAiEgressBlockedError(outgoing.url.host)
        return await async_send(self, outgoing)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    yield attempts
