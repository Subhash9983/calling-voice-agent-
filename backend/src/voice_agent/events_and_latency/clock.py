"""Clock and ID implementations: real ones for runtime, deterministic ones for tests/replays."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


class ManualClock:
    """Deterministic clock advanced explicitly; never sleeps."""

    def __init__(self, start: datetime | None = None, monotonic_ms: int = 0) -> None:
        self._utc = start or datetime(2026, 9, 28, tzinfo=UTC)
        self._monotonic_ms = monotonic_ms

    def utc_now(self) -> datetime:
        return self._utc

    def monotonic_ms(self) -> int:
        return self._monotonic_ms

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("a monotonic clock cannot go backwards")
        self._monotonic_ms += milliseconds
        self._utc += timedelta(milliseconds=milliseconds)


class UuidIdGenerator:
    def new_id(self) -> str:
        return str(uuid.uuid4())


class SequentialIdGenerator:
    """Deterministic canonical UUIDv4-shaped IDs (still opaque; never used for ordering)."""

    def __init__(self, start: int = 1) -> None:
        self._next = start

    def new_id(self) -> str:
        value = uuid.UUID(int=self._next, version=4)
        self._next += 1
        return str(value)
