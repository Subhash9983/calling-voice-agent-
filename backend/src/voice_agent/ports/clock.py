"""Clock and identifier ports (docs/03 §12).

Latency uses the monotonic clock; persisted evidence uses UTC timestamps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def utc_now(self) -> datetime:
        """Current timezone-aware UTC time."""
        ...

    def monotonic_ms(self) -> int:
        """Monotonic process time in milliseconds."""
        ...


@runtime_checkable
class IdGenerator(Protocol):
    def new_id(self) -> str:
        """Return a new canonical UUID string (opaque, never sortable)."""
        ...
