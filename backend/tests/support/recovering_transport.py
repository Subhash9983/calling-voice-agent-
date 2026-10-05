"""Mock control transport with the WP10 recovery capability (tests only; no network).

The development mock transport cannot redispatch a worker, so it stays
non-recoverable; this double adds the ``RecoveryTransportControl`` methods and
a configurable browser presence for the reconciler's recovery path.
"""

from __future__ import annotations

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.events import EventEnvelope
from voice_agent.ports.transport_control import (
    TransportAllocation,
    TransportControlError,
    TransportStatus,
)
from voice_agent.transport_adapters.mock.control import MockTransportControl


class RecoveringMockTransport(MockTransportControl):
    def __init__(self, *, browser_present: bool = True, fail_dispatch: bool = False) -> None:
        super().__init__()
        self.browser_present = browser_present
        self.fail_dispatch = fail_dispatch
        self.recovery_dispatches: dict[str, DispatchLocator] = {}
        self.recovering_notices: list[EventEnvelope] = []

    async def inspect_session(self, allocation: TransportAllocation) -> TransportStatus:
        status = await super().inspect_session(allocation)
        return status.__class__(
            room_exists=status.room_exists,
            browser_present=status.room_exists and self.browser_present,
            agent_present=False,
            participant_count=int(self.browser_present),
            dispatch_present=status.dispatch_present,
        )

    async def ensure_recovery_dispatch(
        self, allocation: TransportAllocation, locator: DispatchLocator, *, agent_name: str
    ) -> str:
        if self.fail_dispatch:
            raise TransportControlError("mock dispatch failure")
        key = locator.recovery_dispatch_id or ""
        self.recovery_dispatches.setdefault(key, locator)
        return f"va-dispatch-recovery-{key}"

    async def notify_recovering(
        self, allocation: TransportAllocation, envelope: EventEnvelope
    ) -> None:
        self.recovering_notices.append(envelope)
