"""Thin room-gateway seam between the session transport and the LiveKit ``rtc`` SDK.

``rtc_binding.RtcRoomGateway`` implements it over ``livekit.rtc.Room``; tests
use an in-process fake. Only primitive values cross this seam (identities,
bytes, flags), so the session-transport logic is testable offline and no SDK
object reaches the application ports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from voice_agent.transport_adapters.livekit.audio import AudioSink


class RoomDisconnectCause(StrEnum):
    """Normalized room disconnect causes (subset of LiveKit ``DisconnectReason``)."""

    CLIENT_INITIATED = "client_initiated"
    DUPLICATE_IDENTITY = "duplicate_identity"
    ROOM_DELETED = "room_deleted"
    PARTICIPANT_REMOVED = "participant_removed"
    SERVER_SHUTDOWN = "server_shutdown"
    CONNECTION_LOST = "connection_lost"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class RoomHandlers:
    """Callbacks the gateway invokes; registered before the room connects."""

    participant_joined: Callable[[str], None]
    participant_left: Callable[[str], None]
    microphone_opened: Callable[[str, AsyncIterator[bytes]], None]
    microphone_closed: Callable[[str], None]
    data_received: Callable[[str | None, str | None, bytes, bool], None]
    reconnecting: Callable[[], None]
    reconnected: Callable[[], None]
    disconnected: Callable[[RoomDisconnectCause], None]


class RoomGateway(Protocol):
    @property
    def local_identity(self) -> str: ...

    async def connect(self, handlers: RoomHandlers) -> None:
        """Register ``handlers``, then join with audio-only auto-subscription."""
        ...

    def remote_identities(self) -> frozenset[str]: ...

    async def publish_agent_audio(self) -> AudioSink:
        """Publish the single ``agent-audio`` 24 kHz mono track (200 ms source queue)."""
        ...

    async def unpublish_agent_audio(self) -> None: ...

    async def publish_data(
        self, payload: bytes, *, reliable: bool, topic: str, destination: str
    ) -> None: ...

    async def disconnect(self) -> None: ...
