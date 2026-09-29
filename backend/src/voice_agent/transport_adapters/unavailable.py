"""Explicit not-ready transport control for providers this build cannot drive.

A ``livekit`` configuration is served by this stand-in when the LiveKit
URL/key/secret are not configured: readiness reports the transport as
unavailable and session creation fails safely before any durable write.
"""

from __future__ import annotations

from datetime import datetime

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.realtime_wire import EndRequestedSignal
from voice_agent.ports.transport_control import (
    JoinCredential,
    TransportAllocation,
    TransportControlError,
    TransportStatus,
)

_UNAVAILABLE = "transport control unavailable"


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

    async def prepare_session(
        self, locator: DispatchLocator, *, agent_name: str
    ) -> TransportAllocation:
        raise TransportControlError(_UNAVAILABLE)

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        raise TransportControlError(_UNAVAILABLE)

    async def inspect_session(self, allocation: TransportAllocation) -> TransportStatus:
        raise TransportControlError(_UNAVAILABLE)

    async def notify_end_requested(
        self, allocation: TransportAllocation, signal: EndRequestedSignal
    ) -> None:
        raise TransportControlError(_UNAVAILABLE)

    async def release_session(self, allocation: TransportAllocation) -> None:
        raise TransportControlError(_UNAVAILABLE)

    async def aclose(self) -> None:
        return None
