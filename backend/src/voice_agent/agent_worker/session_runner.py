"""One admitted LiveKit job, end to end, without AI providers (docs/05 §2-§4, §19-§22; docs/06).

Order: claim generation 1 (duplicate dispatch loses the claim and exits) ->
heartbeat -> connect (listeners first, audio only) -> verify the expected
browser and its microphone within the join timeout -> startup barrier
(re-read the durable termination request) -> ``active``/``listening`` ->
media check until the first stop signal:

- durable or fast-signal end request -> the recorded reason;
- reconnect window expired -> ``browser_closed``/``network_lost`` (ended);
- maximum duration -> ``maximum_duration`` (ended);
- unexpected participant / transport loss -> ``transport_error`` (failed);
- eviction or lease loss -> self-fence: stop output, close, best-effort
  ``worker.self_fenced``, never write the session or rejoin.

Terminal cleanup is ordered and idempotent: stop and clear playback, close
the transport, write ``ending`` then ``ended``/``failed`` under the fenced
writer, emit lifecycle events, release the lease, delete the room
best-effort. The reconciler remains the backstop for every step.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

from voice_agent.agent_worker.admission import JobAdmission, SessionReader
from voice_agent.agent_worker.lease_keeper import HEARTBEAT_INTERVAL_S, LeaseKeeper
from voice_agent.agent_worker.media_check import MediaCheck, MediaMode, MediaTiming
from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.contracts.events import (
    EventEnvelope,
    EventSeverity,
    EventType,
    EventVisibility,
)
from voice_agent.contracts.transport import TransportEvent, TransportEventKind
from voice_agent.domain.control_session import USER_SIDE_END_REASONS
from voice_agent.domain.worker_lease import LeaseToken, WorkerClaim
from voice_agent.events_and_latency.lifecycle import lifecycle_event_id
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.control_plane import EventRecord, SessionEventLog
from voice_agent.ports.repositories import SessionRepository
from voice_agent.ports.session_lifecycle import WorkerLeaseRepository
from voice_agent.ports.transport import SessionTransportPort
from voice_agent.ports.transport_control import TransportAllocation, TransportControl

STEP_TIMEOUT_S: Final = 2.0
WORKER_SERVICE: Final = "agent_worker"
_LOGGER = logging.getLogger(__name__)


class RunOutcome(StrEnum):
    REJECTED = "rejected"
    ENDED = "ended"
    FAILED = "failed"
    SELF_FENCED = "self_fenced"


class _Stop(StrEnum):
    END = "end"
    FENCE = "fence"


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: RunOutcome
    reason: DisconnectReason | None = None


@dataclass(frozen=True, slots=True)
class WorkerStores:
    sessions: SessionReader
    leases: WorkerLeaseRepository
    worker_sessions: Callable[[LeaseToken], SessionRepository]
    events: SessionEventLog
    cleanup: TransportControl | None
    clock: Clock
    ids: IdGenerator


TransportFactory = Callable[[int], SessionTransportPort]


@dataclass(frozen=True, slots=True)
class ActivityContext:
    """What a session activity (media check, STT check, ...) may use; nothing durable."""

    transport: SessionTransportPort
    session_id: str
    correlation_id: str
    worker_generation: int
    lease_hint: Callable[[], int]


ActivityFactory = Callable[[ActivityContext], Awaitable[None]]


async def _attempt(awaitable: Awaitable[object]) -> bool:
    """``True`` when a bounded step completed; failures are normalized to ``False``."""
    try:
        async with asyncio.timeout(STEP_TIMEOUT_S):
            await awaitable
    except Exception:  # every store/transport failure is normalized to "not done"
        _LOGGER.warning("worker.step_failed")
        return False
    return True


async def _bounded[T](awaitable: Awaitable[T]) -> T | None:
    try:
        async with asyncio.timeout(STEP_TIMEOUT_S):
            return await awaitable
    except Exception:  # every store/transport failure is normalized to "not done"
        _LOGGER.warning("worker.step_failed")
        return None


class WorkerSessionRunner:
    def __init__(
        self,
        stores: WorkerStores,
        *,
        admission: JobAdmission,
        claim: WorkerClaim,
        transport_factory: TransportFactory,
        media_mode: MediaMode = MediaMode.TONE,
        heartbeat_s: float | None = None,
        media_timing: MediaTiming | None = None,
        activity: ActivityFactory | None = None,
    ) -> None:
        self._stores = stores
        self._admission = admission
        self._claim = claim
        self._factory = transport_factory
        self._media_mode = media_mode
        self._heartbeat_s = heartbeat_s
        self._media_timing = media_timing or MediaTiming()
        self._activity = activity
        self._session_id = admission.record.session_id
        self._stop: asyncio.Queue[tuple[_Stop, DisconnectReason]] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._joined = False
        self._tasks: set[asyncio.Future[None]] = set()

    # ----------------------------------------------------------------- run --
    async def run(self) -> RunResult:
        token = await _bounded(
            self._stores.leases.claim_initial(
                self._session_id,
                self._claim,
                expected_revision=self._admission.record.state_revision,
                now=self._stores.clock.utc_now(),
            )
        )
        if token is None:
            return RunResult(RunOutcome.REJECTED)
        keeper = self._keeper(token)
        keeper_task = asyncio.create_task(keeper.run())
        try:
            return await self._run_claimed(keeper)
        finally:
            # Graceful stop: never cancel an in-flight store call mid-request.
            keeper.stop()
            try:
                async with asyncio.timeout(STEP_TIMEOUT_S * 2):
                    await keeper_task
            except TimeoutError:
                keeper_task.cancel()

    def _keeper(self, token: LeaseToken) -> LeaseKeeper:
        return LeaseKeeper(
            leases=self._stores.leases,
            sessions=self._stores.sessions,
            token=token,
            clock=self._stores.clock,
            interval_s=HEARTBEAT_INTERVAL_S if self._heartbeat_s is None else self._heartbeat_s,
        )

    async def _run_claimed(self, keeper: LeaseKeeper) -> RunResult:
        transport = self._factory(keeper.token.generation)
        watchers = [
            asyncio.create_task(self._watch_transport(transport)),
            asyncio.create_task(self._watch_keeper(keeper)),
            asyncio.create_task(self._watch_deadline()),
        ]
        try:
            stop, reason = await self._session(transport, keeper)
        finally:
            for task in watchers:
                task.cancel()
        if stop is _Stop.FENCE:
            return await self._self_fence(transport)
        return await self._finalize(transport, keeper, reason)

    async def _session(
        self, transport: SessionTransportPort, keeper: LeaseKeeper
    ) -> tuple[_Stop, DisconnectReason]:
        if not await _attempt(transport.connect()):
            return _Stop.END, DisconnectReason.TRANSPORT_ERROR
        join_ms = self._admission.config.timeout_policy.browser_join_ms
        if not await self._await_ready(join_ms):
            return await self._first_stop(DisconnectReason.TRANSPORT_ERROR)
        barrier = await self._startup_barrier()
        if barrier is not None:
            return _Stop.END, barrier
        if not await self._activate(keeper.token):
            return _Stop.FENCE, DisconnectReason.UNKNOWN
        media = asyncio.create_task(self._media(transport, keeper))
        try:
            return await self._stop.get()
        finally:
            media.cancel()
            with suppress(asyncio.CancelledError):
                await media

    async def _first_stop(self, default: DisconnectReason) -> tuple[_Stop, DisconnectReason]:
        if self._stop.empty():
            return _Stop.END, default
        return self._stop.get_nowait()

    async def _await_ready(self, join_ms: int) -> bool:
        waiter = asyncio.create_task(self._ready.wait())
        stopper = asyncio.create_task(self._peek_stop())
        done, pending = await asyncio.wait(
            {waiter, stopper}, timeout=join_ms / 1000, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        return waiter in done and self._stop.empty()

    async def _peek_stop(self) -> None:
        item = await self._stop.get()
        self._stop.put_nowait(item)

    # ------------------------------------------------------------ signals --
    async def _watch_transport(self, transport: SessionTransportPort) -> None:
        async for event in transport.lifecycle_events():
            self._on_transport_event(event)

    def _on_transport_event(self, event: TransportEvent) -> None:
        kind = event.kind
        if kind is TransportEventKind.BROWSER_JOINED:
            self._joined = True
        elif kind is TransportEventKind.MICROPHONE_READY and self._joined:
            self._ready.set()
        elif kind is TransportEventKind.END_REQUESTED:
            self._spawn(self._verify_end_signal(event.termination_request_revision))
        elif kind is TransportEventKind.RECONNECT_EXPIRED:
            self._stop.put_nowait((_Stop.END, event.reason or DisconnectReason.BROWSER_CLOSED))
        elif kind is TransportEventKind.EVICTED:
            self._stop.put_nowait((_Stop.FENCE, DisconnectReason.UNKNOWN))
        elif kind in (TransportEventKind.UNEXPECTED_PARTICIPANT, TransportEventKind.DISCONNECTED):
            self._stop.put_nowait((_Stop.END, DisconnectReason.TRANSPORT_ERROR))

    def _spawn(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _verify_end_signal(self, revision: int | None) -> None:
        """The fast packet is not authoritative: act only on the matching durable request."""
        record = await _bounded(self._stores.sessions.get(self._session_id))
        request = None if record is None else record.termination_request
        if request is not None and request.revision == revision:
            self._stop.put_nowait((_Stop.END, request.reason))

    async def _watch_keeper(self, keeper: LeaseKeeper) -> None:
        fenced = asyncio.create_task(keeper.fenced.wait())
        ended = asyncio.create_task(keeper.end_requested.wait())
        try:
            await asyncio.wait({fenced, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            fenced.cancel()
            ended.cancel()
        if keeper.fenced.is_set():
            self._stop.put_nowait((_Stop.FENCE, DisconnectReason.UNKNOWN))
        else:
            self._stop.put_nowait((_Stop.END, DisconnectReason.USER_ENDED))

    async def _watch_deadline(self) -> None:
        deadline = self._admission.record.maximum_duration_deadline_at
        remaining = (deadline - self._stores.clock.utc_now()).total_seconds()
        await asyncio.sleep(max(0.0, remaining))
        self._stop.put_nowait((_Stop.END, DisconnectReason.MAXIMUM_DURATION))

    # --------------------------------------------------------- activation --
    async def _startup_barrier(self) -> DisconnectReason | None:
        """Re-read the durable request immediately before activation (docs/05 §3)."""
        record = await _bounded(self._stores.sessions.get(self._session_id))
        if record is None or record.status is not SessionStatus.CONNECTING:
            return DisconnectReason.TRANSPORT_ERROR if record is None else DisconnectReason.UNKNOWN
        request = record.termination_request
        return None if request is None else request.reason

    async def _activate(self, token: LeaseToken) -> bool:
        repository = self._stores.worker_sessions(token)
        session = await _bounded(repository.get(self._session_id))
        if session is None or session.status is not SessionStatus.CONNECTING:
            return False
        if not await _attempt(repository.save(session.transition_to(SessionStatus.ACTIVE))):
            return False
        await self._emit(EventType.SESSION_ACTIVE, deterministic=True)
        await self._emit(EventType.TRANSPORT_CONNECTED, deterministic=True)
        return True

    async def _media(self, transport: SessionTransportPort, keeper: LeaseKeeper) -> None:
        if self._activity is not None:
            await self._activity(
                ActivityContext(
                    transport=transport,
                    session_id=self._session_id,
                    correlation_id=self._admission.record.correlation_id,
                    worker_generation=keeper.token.generation,
                    lease_hint=keeper.lease_valid_for_ms,
                )
            )
            return
        check = MediaCheck(
            transport,
            session_id=self._session_id,
            correlation_id=self._admission.record.correlation_id,
            worker_generation=keeper.token.generation,
            clock=self._stores.clock,
            ids=self._stores.ids,
            mode=self._media_mode,
            lease_hint=keeper.lease_valid_for_ms,
            timing=self._media_timing,
        )
        await check.run()

    # ------------------------------------------------------------ endings --
    async def _self_fence(self, transport: SessionTransportPort) -> RunResult:
        await _attempt(transport.clear_playback())
        await _attempt(transport.close())
        await self._emit(EventType.WORKER_SELF_FENCED, deterministic=False)
        return RunResult(RunOutcome.SELF_FENCED)

    async def _durable_reason(self, fallback: DisconnectReason) -> DisconnectReason:
        record = await _bounded(self._stores.sessions.get(self._session_id))
        request = None if record is None else record.termination_request
        return fallback if request is None else request.reason

    async def _finalize(
        self, transport: SessionTransportPort, keeper: LeaseKeeper, reason: DisconnectReason
    ) -> RunResult:
        await _attempt(transport.clear_playback())
        await _attempt(transport.close())
        final_reason = await self._durable_reason(reason)
        terminal = await self._write_terminal(keeper.token, final_reason)
        await _bounded(keeper.release())
        await self._cleanup_room()
        if terminal is None:
            return RunResult(RunOutcome.SELF_FENCED, final_reason)
        return RunResult(terminal, final_reason)

    async def _write_terminal(
        self, token: LeaseToken, reason: DisconnectReason
    ) -> RunOutcome | None:
        repository = self._stores.worker_sessions(token)
        session = await _bounded(repository.get(self._session_id))
        if session is None or session.is_terminal:
            return None
        target = SessionStatus.ENDED if reason in USER_SIDE_END_REASONS else SessionStatus.FAILED
        if session.status is not SessionStatus.ENDING:
            session = session.transition_to(SessionStatus.ENDING)
            if not await _attempt(repository.save(session)):
                return None
        final = session.transition_to(target, disconnect_reason=reason)
        if not await _attempt(repository.save(final)):
            return None
        event = (
            EventType.SESSION_ENDED if target is SessionStatus.ENDED else EventType.SESSION_FAILED
        )
        await self._emit(event, deterministic=True, payload={"disconnect_reason": reason.value})
        return RunOutcome.ENDED if target is SessionStatus.ENDED else RunOutcome.FAILED

    async def _cleanup_room(self) -> None:
        cleanup, binding = self._stores.cleanup, self._admission.record.transport
        if cleanup is None or binding is None:
            return
        allocation = TransportAllocation(
            provider=binding.provider,
            room_name=binding.external_room_id,
            participant_identity=binding.browser_participant_id,
            dispatch_id=binding.external_session_id,
            agent_identity=binding.agent_participant_id,
        )
        await _attempt(cleanup.release_session(allocation))

    async def _emit(
        self,
        event_type: EventType,
        *,
        deterministic: bool,
        payload: dict[str, JsonValue] | None = None,
    ) -> None:
        clock, record = self._stores.clock, self._admission.record
        envelope = EventEnvelope(
            event_id=(
                lifecycle_event_id(self._session_id, event_type)
                if deterministic
                else self._stores.ids.new_id()
            ),
            event_type=event_type,
            occurred_at=clock.utc_now(),
            session_id=self._session_id,
            correlation_id=record.correlation_id,
            component="worker",
            producer_service=WORKER_SERVICE,
            visibility=EventVisibility.BROWSER_SAFE,
            payload=payload or {},
        )
        severity = (
            EventSeverity.ERROR if event_type is EventType.SESSION_FAILED else EventSeverity.INFO
        )
        await _attempt(self._stores.events.append(EventRecord(envelope, severity, clock.utc_now())))
