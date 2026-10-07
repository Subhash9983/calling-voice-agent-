"""Phase 0 local/trusted access guard (docs/04 §3; docs/12 §6, §10).

Two layers prevent accidental public exposure:

1. ``ensure_loopback_bind`` refuses to serve on anything but ``127.0.0.1``
   with a valid explicit port (no wildcard, IPv6-any, hostname, or random port);
2. ``AccessGuardMiddleware`` rejects, with ``403 ACCESS_FORBIDDEN``, requests
   from a non-loopback peer, with an unexpected ``Host`` (DNS-rebinding
   defence), or from an ``Origin`` other than the exact approved origins.

Phase 0 has no login; a later trusted-network binding needs a separate
authentication decision rather than a configuration change.

Decision 070 (limited-sharing remote deployment) is the single, explicit
exception: only when ``APP_DEPLOYMENT_MODE=remote_limited_sharing`` does
``ensure_approved_bind`` accept ``0.0.0.0``, and ``remote_access_policy``
replace the loopback-peer check (the peer is the hosting proxy) with an exact
check of the backend's own public ``Host`` and the one approved HTTPS
``Origin``. The local default is unchanged.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from voice_agent.control_api.errors import ApiError, ErrorCode
from voice_agent.control_api.middleware import send_error
from voice_agent.control_api.request_context import current_request_id
from voice_agent.security.settings import (
    APPROVED_API_HOST,
    APPROVED_PUBLIC_ORIGINS,
    MAX_PORT,
    REMOTE_BIND_HOST,
    DeploymentMode,
)

LOOPBACK_BIND_HOST: Final = APPROVED_API_HOST
DEFAULT_API_PORT: Final = 8000


class AccessGuardError(RuntimeError):
    """Startup refused: the requested binding is outside the Phase 0 boundary."""


def _ensure_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= MAX_PORT:
        raise AccessGuardError("the control API needs an explicit valid port")


def ensure_loopback_bind(host: str, port: int) -> None:
    if host != LOOPBACK_BIND_HOST or not ipaddress.ip_address(host).is_loopback:
        raise AccessGuardError("the control API binds to 127.0.0.1 only in Phase 0")
    _ensure_port(port)


def ensure_approved_bind(host: str, port: int, mode: DeploymentMode) -> None:
    """Loopback only, except ``0.0.0.0`` in explicit remote mode (Decision 070)."""
    if mode is DeploymentMode.REMOTE_LIMITED_SHARING and host == REMOTE_BIND_HOST:
        _ensure_port(port)
        return
    ensure_loopback_bind(host, port)


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    require_loopback_peer: bool = True


def access_policy(port: int, public_origin: str | None) -> AccessPolicy:
    origins = {f"http://{LOOPBACK_BIND_HOST}:{port}"}
    if public_origin is not None and public_origin in APPROVED_PUBLIC_ORIGINS:
        origins.add(public_origin)
    return AccessPolicy(
        allowed_hosts=frozenset({LOOPBACK_BIND_HOST, f"{LOOPBACK_BIND_HOST}:{port}"}),
        allowed_origins=frozenset(origins),
    )


def remote_access_policy(public_host: str, public_origin: str) -> AccessPolicy:
    """Decision 070: exact public ``Host`` and one HTTPS ``Origin``; no peer check.

    Both values were validated by the settings (lowercase DNS host, exact
    ``https://`` origin, no wildcard). The local origin is *not* added.
    """
    return AccessPolicy(
        allowed_hosts=frozenset({public_host}),
        allowed_origins=frozenset({public_origin}),
        require_loopback_peer=False,
    )


def _is_loopback_peer(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        return ipaddress.ip_address(str(client[0])).is_loopback
    except ValueError:
        return False


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", ()) if key == name]


def is_request_allowed(scope: Scope, policy: AccessPolicy) -> bool:
    if policy.require_loopback_peer and not _is_loopback_peer(scope):
        return False
    hosts = _header_values(scope, b"host")
    if len(hosts) != 1 or hosts[0] not in policy.allowed_hosts:
        return False
    origins = _header_values(scope, b"origin")
    return len(origins) <= 1 and all(origin in policy.allowed_origins for origin in origins)


class AccessGuardMiddleware:
    def __init__(self, app: ASGIApp, *, policy: AccessPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not is_request_allowed(scope, self.policy):
            await send_error(send, ApiError(ErrorCode.ACCESS_FORBIDDEN), current_request_id())
            return
        await self.app(scope, receive, send)
