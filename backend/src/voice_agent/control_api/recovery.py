"""Reconciler-side worker-crash recovery (WP10; docs/05 §21, Decision 067; docs/06 §7, §11).

At most one higher-generation recovery per session:

1. ``start``: one atomic fence + authorization write (``writer_epoch`` + 1,
   ``worker_recovery_count`` + 1, activity ``recovering``, a pre-generated
   ``recovery_dispatch_id``), then best-effort ``agent.recovering`` to the
   browser and the crashed worker's open turns marked ``abandoned`` (no
   buffered speech is replayed);
2. ``continue_``: the owner renews its 10 s ownership lease, verifies the
   browser is still present, and ensures the single replacement dispatch
   (idempotent by ``recovery_dispatch_id``);
3. ``take_over``: after the owner's lease expired (and before the recovery
   deadline) another pass fences again and continues the same recovery;
4. ``fail``: after the 20 s recovery deadline, a missing browser, or a
   second crash, the session is fenced and fails. The replacement worker's
   claim (``claim_recovery``) unsets the authorization on success.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Final

from voice_agent.contracts.dispatch import DispatchLocator
from voice_agent.contracts.enums import AgentActivityState, DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType, EventVisibility
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.services.common import (
    allocation_of,
    emit_session_event,
    release_transport,
    try_replace,
)
from voice_agent.control_api.structured_logging import get_logger, log_event
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.session_reconcile import RecoveryCapability
from voice_agent.ports.session_lifecycle import WorkerRecoveryRepository
from voice_agent.ports.transport_control import RecoveryTransportControl, TransportControlError

RECONCILER_PRODUCER: Final = "control_api"


@dataclass(frozen=True, slots=True)
class _Recovery:
    repository: WorkerRecoveryRepository
    transport: RecoveryTransportControl


class WorkerRecoveryCoordinator:
    def __init__(self, runtime: ControlPlaneRuntime, *, instance_id: str) -> None:
        self._runtime = runtime
        self._instance_id = instance_id
        self.owned: set[str] = set()

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def _parts(self, record: SessionRecord) -> _Recovery | None:
        repository = self._runtime.require_stores().recovery
        binding = record.transport
        transport = None if binding is None else self._runtime.transports.get(binding.provider)
        if repository is None or not isinstance(transport, RecoveryTransportControl):
            return None
        return _Recovery(repository, transport) if transport.is_available else None

    def capability(self, record: SessionRecord) -> RecoveryCapability | None:
        """Recovery is possible only with the recovery store and a capable transport."""
        if self._parts(record) is None:
            return None
        return RecoveryCapability(instance_id=self._instance_id)

    def forget(self, session_id: str) -> None:
        self.owned.discard(session_id)

    # ------------------------------------------------------------- steps --
    async def start(self, record: SessionRecord) -> bool:
        parts = self._parts(record)
        if parts is None:
            return False
        runtime = self._runtime
        now = runtime.clock.utc_now()
        authorization = await runtime.bounded(
            parts.repository.start_recovery(
                record.session_id,
                owner_instance_id=self._instance_id,
                recovery_dispatch_id=runtime.ids.new_id(),
                now=now,
            )
        )
        if authorization is None:
            return False
        self.owned.add(record.session_id)
        await emit_session_event(runtime, record, EventType.WORKER_LEASE_EXPIRED)
        await emit_session_event(
            runtime,
            record,
            EventType.WORKER_RECOVERY_STARTED,
            payload={"owner_generation": authorization.owner_generation},
        )
        await self._notify_recovering(parts, record)
        await self.abandon_open_turns(record)
        return True

    async def take_over(self, record: SessionRecord) -> bool:
        parts, authorization = self._parts(record), record.recovery_authorization
        if parts is None or authorization is None:
            return False
        runtime = self._runtime
        taken = await runtime.bounded(
            parts.repository.take_over(
                record.session_id,
                expected_owner_generation=authorization.owner_generation,
                owner_instance_id=self._instance_id,
                now=runtime.clock.utc_now(),
            )
        )
        if taken is None:
            return False
        self.owned.add(record.session_id)
        log_event(get_logger(), logging.INFO, "reconciler.recovery_taken_over", component="worker")
        return True

    async def continue_(self, record: SessionRecord) -> bool:
        """Renew ownership, check the browser, and ensure the one replacement dispatch."""
        parts, authorization = self._parts(record), record.recovery_authorization
        binding = record.transport
        if parts is None or authorization is None or binding is None:
            return False
        runtime = self._runtime
        renewed = await runtime.bounded(
            parts.repository.renew_ownership(
                record.session_id,
                owner_instance_id=self._instance_id,
                owner_generation=authorization.owner_generation,
                now=runtime.clock.utc_now(),
            )
        )
        if renewed is None:
            self.forget(record.session_id)
            return False
        allocation = allocation_of(binding)
        status = await self._transport_call(parts.transport.inspect_session(allocation))
        if status is not None and not status.browser_present:
            return await self.fail(record, cause="browser_absent")
        locator = DispatchLocator(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            agent_config_id=record.agent_config_id,
            environment="rd" if record.environment.value == "rd" else "development",
            recovery_dispatch_id=renewed.recovery_dispatch_id,
        )
        agent_name = runtime.require_settings().app_agent_name
        await self._transport_call(
            parts.transport.ensure_recovery_dispatch(allocation, locator, agent_name=agent_name)
        )
        return True

    async def fail(self, record: SessionRecord, *, cause: str) -> bool:
        """Exhausted recovery: fence-free terminal write (the epoch is already fenced)."""
        runtime = self._runtime
        final = record.fail(DisconnectReason.TRANSPORT_ERROR, now=runtime.clock.utc_now())
        if not await try_replace(runtime, final, expected_revision=record.state_revision):
            return False
        self.forget(record.session_id)
        await emit_session_event(
            runtime,
            final,
            EventType.WORKER_RECOVERY_FAILED,
            severity=EventSeverity.ERROR,
            payload={"cause": cause},
        )
        await self.abandon_open_turns(final)
        binding = final.transport
        transport = None if binding is None else runtime.transports.get(binding.provider)
        if binding is not None and transport is not None and transport.is_available:
            await release_transport(runtime, transport, allocation_of(binding))
        return True

    async def abandon_open_turns(self, record: SessionRecord) -> None:
        repository = self._runtime.require_stores().recovery
        if repository is None:
            return
        try:
            await self._runtime.bounded(
                repository.abandon_open_turns(record.session_id, now=self._runtime.clock.utc_now())
            )
        except Exception:  # best effort; the turn view stays explainable from events
            log_event(get_logger(), logging.WARNING, "reconciler.turn_abandon_failed")

    # ----------------------------------------------------------- helpers --
    async def _notify_recovering(self, parts: _Recovery, record: SessionRecord) -> None:
        binding = record.transport
        if binding is None:
            return
        runtime = self._runtime
        envelope = EventEnvelope(
            event_id=runtime.ids.new_id(),
            event_type=EventType.AGENT_RECOVERING,
            occurred_at=runtime.clock.utc_now(),
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            component="worker",
            producer_service=RECONCILER_PRODUCER,
            visibility=EventVisibility.BROWSER_SAFE,
            payload={"state": AgentActivityState.RECOVERING.value},
        )
        await self._transport_call(
            parts.transport.notify_recovering(allocation_of(binding), envelope)
        )

    async def _transport_call[T](self, call: Awaitable[T]) -> T | None:
        """A bounded best-effort transport call; failures are logged, never raised."""
        try:
            async with asyncio.timeout(self._runtime.transport_timeout_s):
                return await call
        except (TransportControlError, TimeoutError):
            log_event(get_logger(), logging.WARNING, "reconciler.recovery_transport_failed")
            return None
