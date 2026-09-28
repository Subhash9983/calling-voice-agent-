"""Bounded queues with explicit backpressure behaviour (docs/05 §9).

No wall-clock sleeps: ``asyncio.sleep(0)`` only yields to the scheduler.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_agent.orchestration.queues import (
    BoundedQueue,
    EventOutbox,
    OverflowPolicy,
    QueueClosedError,
    QueueOverflowError,
)


async def _yield() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacity"):
        BoundedQueue[int](capacity=0, policy=OverflowPolicy.BLOCK)


@pytest.mark.asyncio
async def test_block_policy_waits_until_space_frees() -> None:
    queue = BoundedQueue[int](capacity=2, policy=OverflowPolicy.BLOCK)
    await queue.put(1)
    await queue.put(2)

    blocked = asyncio.create_task(queue.put(3))
    await _yield()
    assert not blocked.done()
    assert queue.blocked_puts == 1

    assert await queue.get() == 1
    await _yield()
    assert blocked.done()
    assert [await queue.get(), await queue.get()] == [2, 3]


@pytest.mark.asyncio
async def test_reject_policy_raises_overflow_and_never_drops_silently() -> None:
    queue = BoundedQueue[int](capacity=1, policy=OverflowPolicy.REJECT)
    await queue.put(1)

    with pytest.raises(QueueOverflowError):
        await queue.put(2)

    assert queue.rejected == 1
    assert len(queue) == 1


@pytest.mark.asyncio
async def test_drop_policy_counts_dropped_items() -> None:
    queue = BoundedQueue[int](capacity=1, policy=OverflowPolicy.DROP_NEWEST)
    await queue.put(1)

    accepted = await queue.put(2)

    assert accepted is False
    assert queue.dropped == 1
    assert await queue.get() == 1


@pytest.mark.asyncio
async def test_weighted_capacity_bounds_audio_duration() -> None:
    queue = BoundedQueue[int](capacity=100, policy=OverflowPolicy.REJECT, weigh=lambda ms: ms)
    await queue.put(60)
    await queue.put(40)

    with pytest.raises(QueueOverflowError):
        await queue.put(20)
    assert queue.weight == 100


@pytest.mark.asyncio
async def test_item_heavier_than_capacity_is_rejected_even_when_blocking() -> None:
    queue = BoundedQueue[int](capacity=10, policy=OverflowPolicy.BLOCK, weigh=lambda ms: ms)

    with pytest.raises(QueueOverflowError):
        await queue.put(11)


@pytest.mark.asyncio
async def test_clear_returns_drained_items_in_order_and_wakes_putters() -> None:
    queue = BoundedQueue[str](capacity=2, policy=OverflowPolicy.BLOCK)
    await queue.put("a")
    await queue.put("b")
    blocked = asyncio.create_task(queue.put("c"))
    await _yield()

    drained = queue.clear()
    await _yield()

    assert drained == ["a", "b"]
    assert blocked.done()
    assert await queue.get() == "c"


@pytest.mark.asyncio
async def test_close_wakes_getters_and_rejects_new_items() -> None:
    queue = BoundedQueue[int](capacity=1, policy=OverflowPolicy.BLOCK)
    waiter = asyncio.create_task(queue.get())
    await _yield()

    queue.close()
    await _yield()

    with pytest.raises(QueueClosedError):
        waiter.result()
    with pytest.raises(QueueClosedError):
        await queue.put(1)


@pytest.mark.asyncio
async def test_closed_queue_still_drains_remaining_items() -> None:
    queue = BoundedQueue[int](capacity=2, policy=OverflowPolicy.BLOCK)
    await queue.put(7)
    queue.close()

    assert await queue.get() == 7
    with pytest.raises(QueueClosedError):
        await queue.get()


@pytest.mark.asyncio
async def test_outbox_drops_diagnostics_but_never_terminal_events() -> None:
    outbox = EventOutbox[str](capacity=2)
    assert await outbox.offer("diag-1", terminal=False)
    assert await outbox.offer("diag-2", terminal=False)

    assert not await outbox.offer("diag-3", terminal=False)
    assert await outbox.offer("terminal", terminal=True)

    assert outbox.dropped_diagnostics == 2
    assert outbox.drain() == ["diag-2", "terminal"]


@pytest.mark.asyncio
async def test_outbox_blocks_terminal_event_when_full_of_terminal_events() -> None:
    outbox = EventOutbox[str](capacity=1)
    await outbox.offer("t1", terminal=True)

    pending = asyncio.create_task(outbox.offer("t2", terminal=True))
    await _yield()
    assert not pending.done()

    assert outbox.drain() == ["t1"]
    await _yield()
    assert pending.result() is True
    assert outbox.drain() == ["t2"]
