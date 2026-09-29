"""Deterministic mock control-plane transport (development only; docs/14 §10).

Implements ``TransportControl`` without any network: opaque room, dispatch,
and participant identities plus a non-JWT placeholder credential that no
real transport accepts. Failure switches exercise the create/cleanup paths.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.realtime_wire import EndRequestedSignal
from voice_agent.ports.transport_control import (
    JOIN_TOKEN_LIFETIME_S,
    JoinCredential,
    TransportAllocation,
    TransportControlError,
    TransportStatus,
)

MOCK_TRANSPORT_CONTROL_PROVIDER = "mock_transport"
MOCK_TRANSPORT_URL = "wss://mock-transport.invalid"
MOCK_TOKEN_PREFIX = "mock-join."  # noqa: S105 - marker for a placeholder credential


class MockTransportControl:
    def __init__(
        self,
        *,
        fail_prepare: bool = False,
        fail_token: bool = False,
        fail_release: bool = False,
        fail_notify: bool = False,
    ) -> None:
        self.fail_prepare = fail_prepare
        self.fail_token = fail_token
        self.fail_release = fail_release
        self.fail_notify = fail_notify
        self.prepared: list[str] = []
        self.locators: list[DispatchLocator] = []
        self.released: list[str] = []
        self.signals: list[EndRequestedSignal] = []
        self.tokens_issued = 0
        self.closed = False

    @property
    def provider(self) -> str:
        return MOCK_TRANSPORT_CONTROL_PROVIDER

    @property
    def is_available(self) -> bool:
        return True

    @property
    def public_url(self) -> str:
        return MOCK_TRANSPORT_URL

    async def prepare_session(
        self, locator: DispatchLocator, *, agent_name: str
    ) -> TransportAllocation:
        if self.fail_prepare:
            raise TransportControlError("mock dispatch failure")
        self.prepared.append(locator.session_id)
        self.locators.append(locator)
        return TransportAllocation(
            provider=self.provider,
            room_name=f"va-room-{uuid.uuid4().hex}",
            participant_identity=f"va-browser-{uuid.uuid4().hex}",
            dispatch_id=f"va-dispatch-{uuid.uuid4().hex}",
            agent_identity=f"va-agent-{uuid.uuid4().hex}",
        )

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        if self.fail_token:
            raise TransportControlError("mock token failure")
        self.tokens_issued += 1
        return JoinCredential(
            token=MOCK_TOKEN_PREFIX + secrets.token_urlsafe(24),
            expires_at=now + timedelta(seconds=JOIN_TOKEN_LIFETIME_S),
        )

    async def inspect_session(self, allocation: TransportAllocation) -> TransportStatus:
        exists = allocation.room_name not in self.released
        return TransportStatus(
            room_exists=exists,
            browser_present=False,
            agent_present=False,
            participant_count=0,
            dispatch_present=exists,
        )

    async def notify_end_requested(
        self, allocation: TransportAllocation, signal: EndRequestedSignal
    ) -> None:
        if self.fail_notify:
            raise TransportControlError("mock notify failure")
        self.signals.append(signal)

    async def release_session(self, allocation: TransportAllocation) -> None:
        if self.fail_release:
            raise TransportControlError("mock cleanup failure")
        self.released.append(allocation.room_name)

    async def aclose(self) -> None:
        self.closed = True
