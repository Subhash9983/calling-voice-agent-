"""Control-plane transport port: room/dispatch preparation and join credentials (docs/04 §6-§7).

The control API prepares the backend-owned room, explicit named-agent
dispatch, and opaque browser participant identity, then issues a short-lived
room-scoped join credential. The credential is returned to the browser once
and is never persisted or logged. The LiveKit implementation is WP6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

JOIN_TOKEN_LIFETIME_S = 600


class TransportControlError(RuntimeError):
    """The transport dependency could not complete a control operation."""


@dataclass(frozen=True, slots=True)
class TransportAllocation:
    provider: str
    room_name: str
    participant_identity: str
    dispatch_id: str | None


@dataclass(frozen=True, slots=True)
class JoinCredential:
    # Never repr'd, logged, or persisted (docs/04 §6 join-token rules).
    token: str = field(repr=False)
    expires_at: datetime


@runtime_checkable
class TransportControl(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def is_available(self) -> bool:
        """``False`` when this build cannot perform control operations."""
        ...

    @property
    def public_url(self) -> str:
        """Browser-safe server URL the participant joins (no credentials)."""
        ...

    async def prepare_session(self, *, session_id: str, agent_name: str) -> TransportAllocation:
        """Create the opaque room and explicit agent dispatch for a durable session."""
        ...

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        """Issue a room/participant-scoped credential valid for 10 minutes."""
        ...

    async def release_session(self, allocation: TransportAllocation) -> None:
        """Best-effort dispatch/room cleanup after a failed creation."""
        ...
