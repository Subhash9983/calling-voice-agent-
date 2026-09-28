"""Explicit not-ready transport control for providers this build cannot drive yet.

Until the LiveKit control adapter lands (WP6), a ``livekit`` configuration is
served by this stand-in: readiness reports the transport as unavailable and
session creation fails safely before any durable write.
"""

from __future__ import annotations

from datetime import datetime

from voice_agent.ports.transport_control import (
    JoinCredential,
    TransportAllocation,
    TransportControlError,
)


class UnavailableTransportControl:
    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def is_available(self) -> bool:
        return False

    @property
    def public_url(self) -> str:
        return ""

    async def prepare_session(self, *, session_id: str, agent_name: str) -> TransportAllocation:
        raise TransportControlError("transport control unavailable")

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        raise TransportControlError("transport control unavailable")

    async def release_session(self, allocation: TransportAllocation) -> None:
        raise TransportControlError("transport control unavailable")
