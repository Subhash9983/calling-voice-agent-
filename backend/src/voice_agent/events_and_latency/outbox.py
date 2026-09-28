"""Bounded durable-event outbox and non-blocking publisher (docs/02 §9; docs/03 §19).

A durable-event append that fails transiently is not lost: it waits in a
bounded in-process outbox and is retried with capped exponential backoff,
keeping its ``event_id`` so the store's unique index deduplicates any
replay. A retried append is recorded as late. Terminal session/turn events
get more attempts than diagnostic ones. A permanently rejected or exhausted
event is counted and reported, and lifecycle events remain reconcilable
from session state (deterministic IDs, ``lifecycle.py``).

``BufferedEventPublisher`` gives the worker the same guarantee without
putting database latency on the realtime path: it never awaits the store.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Final

from voice_agent.contracts.events import EventEnvelope, EventSeverity
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import EventRecord, StoreUnavailableError
from voice_agent.ports.repositories import EventSequenceAllocator, SessionEventRepository

DEFAULT_CAPACITY: Final = 500
Append = Callable[[EventRecord], Awaitable[None]]
DropHook = Callable[[EventRecord, str], None]
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (StoreUnavailableError, TimeoutError)


@dataclass(frozen=True, slots=True)
class OutboxPolicy:
    capacity: int = DEFAULT_CAPACITY
    terminal_attempts: int = 6
    diagnostic_attempts: int = 3
    initial_backoff_s: float = 0.25
    maximum_backoff_s: float = 2.0
    append_timeout_s: float = 2.0

    def attempts_for(self, record: EventRecord) -> int:
        return self.terminal_attempts if record.envelope.is_terminal else self.diagnostic_attempts

    def backoff(self, attempt: int) -> float:
        return float(min(self.maximum_backoff_s, self.initial_backoff_s * (2 ** (attempt - 1))))


@dataclass(slots=True)
class _Pending:
    record: EventRecord
    attempts: int = 0


@dataclass(slots=True)
class OutboxStats:
    delivered: int = 0
    retried: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


class DurableEventOutbox:
    def __init__(
        self,
        append: Append,
        *,
        clock: Clock,
        policy: OutboxPolicy | None = None,
        on_drop: DropHook | None = None,
    ) -> None:
        self._append = append
        self._clock = clock
        self._policy = policy or OutboxPolicy()
        self._on_drop = on_drop
        self._queue: deque[_Pending] = deque()
        self._wakeup = asyncio.Event()
        self.stats = OutboxStats()

    @property
    def pending(self) -> int:
        return len(self._queue)

    def defer(self, record: EventRecord, *, attempts: int = 0) -> None:
        """Queue a record; on overflow the oldest non-terminal record is dropped first."""
        if len(self._queue) >= self._policy.capacity:
            victim = next((p for p in self._queue if not p.record.envelope.is_terminal), None)
            victim = victim or self._queue[0]
            self._queue.remove(victim)
            self._drop(victim.record, "capacity")
        self._queue.append(_Pending(record, attempts))
        self._wakeup.set()

    async def submit(self, record: EventRecord) -> None:
        """Try once now (bounded); on a transient failure defer instead of raising."""
        try:
            async with asyncio.timeout(self._policy.append_timeout_s):
                await self._append(record)
        except TRANSIENT_ERRORS:
            self.defer(record, attempts=1)
            return
        self.stats.delivered += 1

    async def drain_once(self) -> int:
        """Attempt every queued record once in FIFO order; returns deliveries."""
        delivered = 0
        for _ in range(len(self._queue)):
            pending = self._queue.popleft()
            if await self._attempt(pending):
                delivered += 1
        return delivered

    async def _attempt(self, pending: _Pending) -> bool:
        pending.attempts += 1
        retry = pending.attempts > 1
        record = self._as_late(pending.record) if retry else pending.record
        try:
            async with asyncio.timeout(self._policy.append_timeout_s):
                await self._append(record)
        except TRANSIENT_ERRORS:
            if pending.attempts >= self._policy.attempts_for(pending.record):
                self._drop(pending.record, "attempts_exhausted")
            else:
                self._queue.append(pending)
            return False
        except Exception:
            self._drop(pending.record, "rejected")
            return False
        self.stats.retried += 1 if retry else 0
        self.stats.delivered += 1
        return True

    def _as_late(self, record: EventRecord) -> EventRecord:
        late = self._clock.utc_now() - record.envelope.occurred_at
        return replace(record, late_by_ms=max(0, late // timedelta(milliseconds=1)))

    def _drop(self, record: EventRecord, reason: str) -> None:
        self.stats.drop(reason)
        if self._on_drop is not None:
            self._on_drop(record, reason)

    def wake(self) -> None:
        self._wakeup.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Background drain loop: immediate on new work, capped backoff after failures."""
        failures = 0
        while not stop.is_set():
            if not self._queue:
                await self._wait(self._policy.maximum_backoff_s, stop)
                continue
            delivered = await self.drain_once()
            if self._queue and delivered == 0:
                failures += 1
                await self._wait(self._policy.backoff(failures), stop)
            else:
                failures = 0

    async def _wait(self, seconds: float, stop: asyncio.Event) -> None:
        self._wakeup.clear()
        if stop.is_set():  # a stop signalled before the clear must not be lost
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wakeup.wait()


class BufferedEventPublisher:
    """Worker ``EventPublisher`` that never blocks the realtime path on the store.

    Durable events are allocated and appended by a background task in
    publication order; ``flush`` drains at session finalization.
    """

    def __init__(
        self,
        *,
        allocator: EventSequenceAllocator,
        repository: SessionEventRepository,
        clock: Clock,
        policy: OutboxPolicy | None = None,
        max_timeline_events: int = 5000,
    ) -> None:
        self._allocator = allocator
        self._repository = repository
        self._outbox = DurableEventOutbox(self._append, clock=clock, policy=policy)
        self._clock = clock
        self._timeline: list[EventEnvelope] = []
        self._max_timeline = max_timeline_events
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def outbox(self) -> DurableEventOutbox:
        return self._outbox

    @property
    def timeline(self) -> tuple[EventEnvelope, ...]:
        return tuple(self._timeline)

    async def publish(self, envelope: EventEnvelope) -> EventEnvelope:
        if envelope.is_durable:
            record = EventRecord(envelope, EventSeverity.INFO, self._clock.utc_now())
            self._outbox.defer(record)
            self._ensure_running()
        if len(self._timeline) < self._max_timeline:
            self._timeline.append(envelope)
        return envelope

    async def _append(self, record: EventRecord) -> None:
        sequence = await self._allocator.next_sequence(record.envelope.session_id)
        envelope = record.envelope.model_copy(update={"sequence_number": sequence})
        await self._repository.append(envelope)

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._outbox.run(self._stop))

    async def flush(self, *, rounds: int = 10) -> None:
        """Deliver what the store accepts; transient failures keep their retry budget."""
        for _ in range(rounds):
            if not self._outbox.pending:
                break
            await self._outbox.drain_once()

    async def aclose(self) -> None:
        self._stop.set()
        self._outbox.wake()
        if self._task is not None:
            await self._task
