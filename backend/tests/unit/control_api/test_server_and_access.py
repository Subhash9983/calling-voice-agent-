"""Fail-safe startup and the local access guard (docs/04 §3; docs/12 §6; docs/14 §21)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI

from voice_agent.control_api import server
from voice_agent.control_api.access import (
    AccessGuardError,
    access_policy,
    ensure_loopback_bind,
    is_request_allowed,
)
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID

READY_ENV = {"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID}


class FakeServe:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, app: Any, **kwargs: Any) -> None:
        self.calls.append((app, kwargs))


@pytest.fixture(autouse=True)
def _no_logging_reconfiguration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep pytest's log capture intact for other tests.
    monkeypatch.setattr(server, "configure_logging", lambda _level: None)


def test_main_serves_only_on_loopback_without_access_log() -> None:
    serve = FakeServe()

    code = server.main([], environ=READY_ENV, serve=serve)

    assert code == server.EXIT_OK
    ((app, options),) = serve.calls
    assert isinstance(app, FastAPI)
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8000
    assert options["access_log"] is False
    assert options["proxy_headers"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"APP_API_HOST": "0.0.0.0"},  # noqa: S104 - the binding under test
        {"APP_API_HOST": "::"},
        {"APP_API_HOST": "localhost"},
        {"APP_API_HOST": "192.168.1.10"},
        {"APP_API_PORT": "0"},
        {"APP_ENV": "production"},
        {"APP_PUBLIC_ORIGIN": "*"},
    ],
    ids=["any_ipv4", "any_ipv6", "hostname", "lan", "random_port", "production", "wildcard"],
)
def test_main_refuses_unsafe_configuration_before_serving(
    overrides: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    serve = FakeServe()

    code = server.main([], environ={**READY_ENV, **overrides}, serve=serve)

    assert code == server.EXIT_REFUSED
    assert serve.calls == []
    output = json.loads(capsys.readouterr().err)
    assert output["started"] is False
    for value in overrides.values():
        if value not in {"0", "*"}:
            assert value not in json.dumps(output)


def test_main_refusal_when_guard_rejects_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject(_host: str, _port: int) -> None:
        raise AccessGuardError("refused")

    monkeypatch.setattr(server, "ensure_loopback_bind", reject)
    serve = FakeServe()

    assert server.main([], environ=READY_ENV, serve=serve) == server.EXIT_REFUSED
    assert serve.calls == []
    assert json.loads(capsys.readouterr().err) == {
        "started": False,
        "reason": "binding_not_allowed",
    }


def test_main_accepts_mongodb_persistence_flag() -> None:
    serve = FakeServe()

    assert server.main(["--persistence", "mongodb"], environ=READY_ENV, serve=serve) == 0
    assert len(serve.calls) == 1


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("0.0.0.0", 8000),  # noqa: S104
        ("::", 8000),
        ("::1", 8000),
        ("127.0.0.2", 8000),
        ("localhost", 8000),
        ("127.0.0.1", 0),
        ("127.0.0.1", 70_000),
        ("127.0.0.1", True),
    ],
)
def test_loopback_bind_guard_rejects(host: str, port: int) -> None:
    with pytest.raises((AccessGuardError, ValueError)):
        ensure_loopback_bind(host, port)


def test_loopback_bind_guard_accepts_the_approved_binding() -> None:
    ensure_loopback_bind("127.0.0.1", 8000)


def _scope(client: tuple[str, int] | None, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "client": client, "headers": headers}


POLICY = access_policy(8000, "http://127.0.0.1:5173")
HOST = (b"host", b"127.0.0.1:8000")


@pytest.mark.parametrize(
    ("client", "headers", "allowed"),
    [
        (("127.0.0.1", 1), [HOST], True),
        (("127.0.0.1", 1), [HOST, (b"origin", b"http://127.0.0.1:5173")], True),
        (("127.0.0.1", 1), [HOST, (b"origin", b"http://127.0.0.1:8000")], True),
        (None, [HOST], False),
        (("not-an-ip", 1), [HOST], False),
        (("127.0.0.1", 1), [], False),
        (("127.0.0.1", 1), [HOST, HOST], False),
        (("127.0.0.1", 1), [HOST, (b"origin", b"a"), (b"origin", b"b")], False),
    ],
    ids=["plain", "frontend", "self", "no_peer", "bad_peer", "no_host", "two_hosts", "origins"],
)
def test_request_access_policy(
    client: tuple[str, int] | None, headers: list[tuple[bytes, bytes]], allowed: bool
) -> None:
    assert is_request_allowed(_scope(client, headers), POLICY) is allowed


def test_policy_ignores_unapproved_public_origin() -> None:
    policy = access_policy(8000, "https://attacker.example")

    assert policy.allowed_origins == frozenset({"http://127.0.0.1:8000"})
