"""Ordered, off-the-hot-path evidence writes for the speech pipeline (docs/05 §8).

Durable evidence (operation rows, timeline events, turn saves) must never
stall audio publication, yet an operation's ``started`` row must not land
after its terminal row. Writes are submitted as factories and executed one
at a time, in submission order, by a single background task. Each write is
already bounded and normalized by :class:`SttEvidence`; a failing write is
logged by class name only and never stops later writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Final

WriteFactory = Callable[[], Awaitable[object]]
CLOSE_TIMEOUT_S: Final = 5.0
_LOGGER = logging.getLogger("voice_agent.agent_worker.tts")


class OrderedWriter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[WriteFactory | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.failed = 0

    def submit(self, factory: WriteFactory) -> None:
        if self._closed:
            return
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
        self._queue.put_nowait(factory)

    async def _run(self) -> None:
        while (factory := await self._queue.get()) is not None:
            try:
                await factory()
            except Exception as error:  # evidence never blocks realtime work
                self.failed += 1
                _LOGGER.warning(
                    "tts.evidence_write_failed",
                    extra={"safe_fields": {"error": type(error).__name__}},
                )
            finally:
                self._queue.task_done()
        self._queue.task_done()

    async def flush(self) -> None:
        """Wait until every write submitted so far has finished."""
        if self._task is not None:
            await self._queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is None:
            return
        self._queue.put_nowait(None)
        with suppress(TimeoutError):
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await task
        if not task.done():
            task.cancel()
