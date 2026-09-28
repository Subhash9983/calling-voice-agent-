"""Bounded queues with explicit overflow behaviour (docs/05 §9).

Unbounded queues are prohibited. Policies:

- ``BLOCK``: the producer waits (segment and playback queues: backpressure);
- ``REJECT``: raise :class:`QueueOverflowError` so the caller records
  ``realtime_overload`` evidence (inbound audio is never silently dropped);
- ``DROP_NEWEST``: drop with a recorded counter (optional diagnostics only).

Capacity can be item-count or weighted (e.g. milliseconds of audio), because
the time-based caps are authoritative. Waiters are woken synchronously, so
``clear()`` and ``close()`` are safe to call from non-async code.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from enum import StrEnum


class OverflowPolicy(StrEnum):
    BLOCK = "block"
    REJECT = "reject"
    DROP_NEWEST = "drop_newest"


class QueueOverflowError(RuntimeError):
    """A bounded queue is full and its policy rejects new items."""


class QueueClosedError(RuntimeError):
    """The queue is closed and (for getters) empty."""


def _unit_weight(_item: object) -> int:
    return 1


class _Waiters:
    """FIFO of futures woken all at once; each waiter re-checks its condition."""

    def __init__(self) -> None:
        self._futures: deque[asyncio.Future[None]] = deque()

    async def wait(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._futures.append(future)
        try:
            await future
        finally:
            if future in self._futures:
                self._futures.remove(future)

    def wake_all(self) -> None:
        while self._futures:
            future = self._futures.popleft()
            if not future.done():
                future.set_result(None)


class BoundedQueue[T]:
    def __init__(
        self,
        *,
        capacity: int,
        policy: OverflowPolicy,
        weigh: Callable[[T], int] = _unit_weight,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._policy = policy
        self._weigh = weigh
        self._items: deque[tuple[T, int]] = deque()
        self._weight = 0
        self._closed = False
        self._getters = _Waiters()
        self._putters = _Waiters()
        self.blocked_puts = 0
        self.rejected = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def weight(self) -> int:
        return self._weight

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _fits(self, weight: int) -> bool:
        return self._weight + weight <= self._capacity

    async def put(self, item: T) -> bool:
        """Enqueue ``item``; returns ``False`` only when a ``DROP_NEWEST`` drop occurs."""
        weight = self._weigh(item)
        if weight > self._capacity:
            self.rejected += 1
            raise QueueOverflowError("item exceeds the queue capacity")
        self._raise_if_closed()
        if not self._fits(weight):
            if self._policy is OverflowPolicy.REJECT:
                self.rejected += 1
                raise QueueOverflowError("bounded queue is full")
            if self._policy is OverflowPolicy.DROP_NEWEST:
                self.dropped += 1
                return False
            self.blocked_puts += 1
            while not self._closed and not self._fits(weight):
                await self._putters.wait()
            self._raise_if_closed()
        self._items.append((item, weight))
        self._weight += weight
        self._getters.wake_all()
        return True

    async def get(self) -> T:
        while not self._items:
            if self._closed:
                raise QueueClosedError("queue is closed")
            await self._getters.wait()
        item, weight = self._items.popleft()
        self._weight -= weight
        self._putters.wake_all()
        return item

    def clear(self) -> list[T]:
        """Remove and return every queued item in FIFO order, waking blocked putters."""
        drained = [item for item, _ in self._items]
        self._items.clear()
        self._weight = 0
        self._putters.wake_all()
        return drained

    def close(self) -> None:
        self._closed = True
        self._getters.wake_all()
        self._putters.wake_all()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise QueueClosedError("queue is closed")


class EventOutbox[T]:
    """Non-terminal diagnostics drop with a counter; terminal events are never dropped.

    A terminal event evicts the oldest diagnostic when full; if the outbox is
    full of terminal events it waits (bounded, reliable persistence).
    """

    def __init__(self, *, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: deque[tuple[T, bool]] = deque()
        self._space = _Waiters()
        self.dropped_diagnostics = 0

    def __len__(self) -> int:
        return len(self._items)

    async def offer(self, item: T, *, terminal: bool) -> bool:
        if len(self._items) >= self._capacity:
            if not terminal:
                self.dropped_diagnostics += 1
                return False
            if not self._evict_oldest_diagnostic():
                while len(self._items) >= self._capacity:
                    await self._space.wait()
        self._items.append((item, terminal))
        return True

    def _evict_oldest_diagnostic(self) -> bool:
        for index, (_item, is_terminal) in enumerate(self._items):
            if not is_terminal:
                del self._items[index]
                self.dropped_diagnostics += 1
                return True
        return False

    def drain(self) -> list[T]:
        drained = [item for item, _ in self._items]
        self._items.clear()
        self._space.wake_all()
        return drained
