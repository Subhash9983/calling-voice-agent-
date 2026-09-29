"""Bounded prompt-reconciliation nudges (docs/04 §9: no waiting for an absent worker)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

MAX_NUDGES: Final = 100


@dataclass
class ReconcileNudges:
    """Session IDs to reconcile on the next pass; overflow simply waits for the scan."""

    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(MAX_NUDGES))
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)

    def nudge(self, session_id: str) -> None:
        with suppress(asyncio.QueueFull):
            self.queue.put_nowait(session_id)
        self.wakeup.set()

    def drain(self) -> tuple[str, ...]:
        items: list[str] = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        self.wakeup.clear()
        return tuple(dict.fromkeys(items))
