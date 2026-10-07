"""Decision 070 bind guard and remote access policy (local behavior unchanged)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from voice_agent.control_api import server
from voice_agent.control_api.access import (
    AccessGuardError,
    access_policy,
    ensure_approved_bind,
    is_request_allowed,
    remote_access_policy,
)
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID
from voice_agent.security.settings import DeploymentMode

ANY_IPV4 = "0.0.0.0"  # noqa: S104 - the binding under test
LOCAL = DeploymentMode.LOCAL
REMOTE = DeploymentMode.REMOTE_LIMITED_SHARING
ORIGIN = "https://voice-agent-web.onrender.com"
API_HOST = "voice-agent-api.onrender.com"
REMOTE_ENV = {
    "APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID,
    "APP_DEPLOYMENT_MODE": "remote_limited_sharing",
    "APP_API_HOST": ANY_IPV4,
    "APP_API_PORT": "10000",
    "APP_PUBLIC_ORIGIN": ORIGIN,
    "APP_API_PUBLIC_HOST": API_HOST,
}


class FakeServe:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, app: Any, **kwargs: Any) -> None:
        self.calls.append((app, kwargs))


@pytest.fixture(autouse=True)
def _no_logging_reconfiguration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "configure_logging", lambda _level: None)


# ------------------------------------------------------- bind guard (4x) --


def test_local_mode_accepts_the_loopback_bind() -> None:
    ensure_approved_bind("127.0.0.1", 8000, LOCAL)


def test_local_mode_rejects_bind_all() -> None:
    with pytest.raises(AccessGuardError):
        ensure_approved_bind(ANY_IPV4, 8000, LOCAL)


def test_remote_mode_accepts_bind_all() -> None:
    ensure_approved_bind(ANY_IPV4, 10000, REMOTE)


def test_remote_mode_accepts_the_loopback_bind() -> None:
    ensure_approved_bind("127.0.0.1", 10000, REMOTE)


@pytest.mark.parametrize("mode", [LOCAL, REMOTE])
@pytest.mark.parametrize(
    ("host", "port"),
    [("::", 8000), ("localhost", 8000), ("10.0.0.5", 8000), (ANY_IPV4, 0), (ANY_IPV4, True)],
)
def test_bind_guard_rejects_other_hosts_and_ports_in_both_modes(
    mode: DeploymentMode, host: str, port: int
) -> None:
    with pytest.raises((AccessGuardError, ValueError)):
        ensure_approved_bind(host, port, mode)


# -------------------------------------------------------- remote policy --


def _scope(client: tuple[str, int] | None, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "client": client, "headers": headers}


REMOTE_POLICY = remote_access_policy(API_HOST, ORIGIN)
PUBLIC_HOST = (b"host", API_HOST.encode())
PROXY_PEER = ("10.214.3.7", 41000)


@pytest.mark.parametrize(
    ("client", "headers", "allowed"),
    [
        (PROXY_PEER, [PUBLIC_HOST], True),
        (PROXY_PEER, [PUBLIC_HOST, (b"origin", ORIGIN.encode())], True),
        (None, [PUBLIC_HOST], True),
        (PROXY_PEER, [(b"host", b"127.0.0.1:8000")], False),
        (PROXY_PEER, [(b"host", b"evil.example")], False),
        (PROXY_PEER, [], False),
        (PROXY_PEER, [PUBLIC_HOST, PUBLIC_HOST], False),
        (PROXY_PEER, [PUBLIC_HOST, (b"origin", b"http://127.0.0.1:5173")], False),
        (PROXY_PEER, [PUBLIC_HOST, (b"origin", b"https://evil.example")], False),
        (PROXY_PEER, [PUBLIC_HOST, (b"origin", b"http://voice-agent-web.onrender.com")], False),
    ],
    ids=[
        "proxied",
        "frontend",
        "no_peer",
        "loopback_host",
        "foreign_host",
        "no_host",
        "two_hosts",
        "local_origin",
        "foreign_origin",
        "http_origin",
    ],
)
def test_remote_policy_checks_host_and_origin_but_not_the_proxy_peer(
    client: tuple[str, int] | None, headers: list[tuple[bytes, bytes]], allowed: bool
) -> None:
    assert is_request_allowed(_scope(client, headers), REMOTE_POLICY) is allowed


def test_local_policy_still_requires_a_loopback_peer() -> None:
    policy = access_policy(8000, "http://127.0.0.1:5173")
    headers = [(b"host", b"127.0.0.1:8000")]

    assert policy.require_loopback_peer is True
    assert is_request_allowed(_scope(PROXY_PEER, headers), policy) is False


# ---------------------------------------------------------------- server --


def test_server_binds_all_interfaces_only_in_remote_mode_with_mongodb() -> None:
    serve = FakeServe()

    code = server.main(["--persistence", "mongodb"], environ=REMOTE_ENV, serve=serve)

    assert code == server.EXIT_OK
    ((_app, options),) = serve.calls
    assert options["host"] == ANY_IPV4
    assert options["port"] == 10000
    assert options["access_log"] is False
    assert options["proxy_headers"] is False


def test_server_refuses_remote_mode_with_in_memory_persistence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve = FakeServe()

    code = server.main([], environ=REMOTE_ENV, serve=serve)

    assert code == server.EXIT_REFUSED
    assert serve.calls == []
    assert json.loads(capsys.readouterr().err) == {
        "started": False,
        "reason": "persistence_not_allowed",
    }


def test_server_still_refuses_bind_all_in_local_mode(capsys: pytest.CaptureFixture[str]) -> None:
    serve = FakeServe()
    environ = {"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID, "APP_API_HOST": ANY_IPV4}

    assert server.main([], environ=environ, serve=serve) == server.EXIT_REFUSED
    assert serve.calls == []
    assert json.loads(capsys.readouterr().err)["started"] is False
