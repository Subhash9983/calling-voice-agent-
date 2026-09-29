"""Control-plane transport port: room/dispatch, join credentials, inspection, cleanup.

docs/04 §6-§9, docs/06 §5-§6, §17. The control API prepares the backend-owned
room, explicit named-agent dispatch, and opaque browser/agent participant
identities, then issues a short-lived room-scoped join credential. The
credential is returned to the browser once and is never persisted or logged.
Implementations normalize every provider failure to
:class:`TransportControlError` without provider messages or payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.realtime_wire import EndRequestedSignal

JOIN_TOKEN_LIFETIME_S = 600


class TransportControlError(RuntimeError):
    """The transport dependency could not complete a control operation.

    ``code`` is a stable safe diagnostic (docs/06 §19); the message never
    carries provider text, payloads, tokens, or addresses.
    """

    def __init__(
        self,
        message: str = "transport control failed",
        *,
        code: str = "transport_unavailable",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TransportAllocation:
    provider: str
    room_name: str
    participant_identity: str
    dispatch_id: str | None
    # Opaque expected agent identity (docs/06 §7); ``None`` for transports
    # without a separate agent participant (the offline mock).
    agent_identity: str | None = None


@dataclass(frozen=True, slots=True)
class JoinCredential:
    # Never repr'd, logged, or persisted (docs/04 §6 join-token rules).
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransportStatus:
    """Safe room/dispatch inspection summary (docs/06 §6, §17); no provider objects."""

    room_exists: bool
    browser_present: bool
    agent_present: bool
    participant_count: int
    dispatch_present: bool | None = None


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

    async def prepare_session(
        self, locator: DispatchLocator, *, agent_name: str
    ) -> TransportAllocation:
        """Create the opaque room and explicit agent dispatch for a durable session."""
        ...

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        """Issue a room/participant-scoped credential valid for 10 minutes."""
        ...

    async def inspect_session(self, allocation: TransportAllocation) -> TransportStatus:
        """Read safe room/participant/dispatch status."""
        ...

    async def notify_end_requested(
        self, allocation: TransportAllocation, signal: EndRequestedSignal
    ) -> None:
        """Send the targeted reliable ``va.control.v1`` wake-up to the agent identity."""
        ...

    async def release_session(self, allocation: TransportAllocation) -> None:
        """Best-effort dispatch/room cleanup (failed creation or terminal session)."""
        ...

    async def aclose(self) -> None:
        """Close control-plane resources idempotently."""
        ...
