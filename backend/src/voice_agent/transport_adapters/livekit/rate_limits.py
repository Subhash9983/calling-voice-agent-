"""Browser-to-agent message limits (docs/06 §13).

All ``va.client.v1`` messages from the browser participant share one token
bucket of 20 messages/second with a burst of 40; ``playback.progress`` is
additionally limited to one every 250 ms. Excess lossy messages are dropped
with a counter, excess reliable messages are rejected (safe
``realtime_overload`` evidence), and sustained abuse blocks further client
events for the session without growing payload or log volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

AGGREGATE_RATE_PER_S: Final = 20
AGGREGATE_BURST: Final = 40
PROGRESS_INTERVAL_MS: Final = 250
# Over-limit messages tolerated before client events are blocked for the session.
SUSTAINED_ABUSE_LIMIT: Final = 100
_MS_PER_S: Final = 1000


@dataclass
class TokenBucket:
    rate_per_s: float
    burst: int
    now_ms: int
    tokens: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    def try_take(self, now_ms: int) -> bool:
        elapsed = max(0, now_ms - self.now_ms)
        self.now_ms = max(self.now_ms, now_ms)
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate_per_s / _MS_PER_S)
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


class GateVerdict(StrEnum):
    ACCEPT = "accept"
    DROP = "drop"
    REJECT = "reject"
    BLOCKED = "blocked"


@dataclass
class ClientMessageGate:
    now_ms: int
    aggregate: TokenBucket = field(init=False)
    last_progress_ms: int | None = None
    over_limit: int = 0
    rejected: int = 0
    dropped: int = 0
    progress_dropped: int = 0
    blocked: bool = False

    def __post_init__(self) -> None:
        self.aggregate = TokenBucket(AGGREGATE_RATE_PER_S, AGGREGATE_BURST, self.now_ms)

    def admit(self, *, reliable: bool, progress: bool, now_ms: int) -> GateVerdict:
        if self.blocked:
            return GateVerdict.BLOCKED
        if progress and not self._progress_due(now_ms):
            self.progress_dropped += 1
            return GateVerdict.DROP
        if self.aggregate.try_take(now_ms):
            if progress:
                self.last_progress_ms = now_ms
            return GateVerdict.ACCEPT
        return self._over_limit(reliable=reliable)

    def _progress_due(self, now_ms: int) -> bool:
        last = self.last_progress_ms
        return last is None or now_ms - last >= PROGRESS_INTERVAL_MS

    def _over_limit(self, *, reliable: bool) -> GateVerdict:
        self.over_limit += 1
        if self.over_limit > SUSTAINED_ABUSE_LIMIT:
            self.blocked = True
            return GateVerdict.BLOCKED
        if reliable:
            self.rejected += 1
            return GateVerdict.REJECT
        self.dropped += 1
        return GateVerdict.DROP
