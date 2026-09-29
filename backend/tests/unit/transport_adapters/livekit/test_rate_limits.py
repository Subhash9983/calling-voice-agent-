"""Browser message limits: 20/s aggregate (burst 40) and progress at most every 250 ms."""

from __future__ import annotations

from voice_agent.transport_adapters.livekit.rate_limits import (
    AGGREGATE_BURST,
    AGGREGATE_RATE_PER_S,
    PROGRESS_INTERVAL_MS,
    SUSTAINED_ABUSE_LIMIT,
    ClientMessageGate,
    GateVerdict,
    TokenBucket,
)


def test_bucket_allows_burst_then_refills_at_rate() -> None:
    bucket = TokenBucket(rate_per_s=AGGREGATE_RATE_PER_S, burst=AGGREGATE_BURST, now_ms=0)

    burst = [bucket.try_take(0) for _ in range(AGGREGATE_BURST + 1)]
    refilled = [bucket.try_take(1000) for _ in range(AGGREGATE_RATE_PER_S + 1)]

    assert burst.count(True) == AGGREGATE_BURST
    assert burst[-1] is False
    assert refilled.count(True) == AGGREGATE_RATE_PER_S


def test_bucket_never_exceeds_burst_after_idle() -> None:
    bucket = TokenBucket(rate_per_s=20, burst=40, now_ms=0)

    allowed = sum(bucket.try_take(60_000) for _ in range(100))

    assert allowed == 40


def test_bucket_ignores_clock_going_backwards() -> None:
    bucket = TokenBucket(rate_per_s=1, burst=1, now_ms=1000)

    assert bucket.try_take(1000)
    assert not bucket.try_take(0)


def test_sustained_20_per_second_is_accepted() -> None:
    gate = ClientMessageGate(now_ms=0)

    verdicts = [gate.admit(reliable=True, progress=False, now_ms=i * 50) for i in range(200)]

    assert set(verdicts) == {GateVerdict.ACCEPT}


def test_excess_reliable_is_rejected_and_lossy_dropped() -> None:
    gate = ClientMessageGate(now_ms=0)
    for _ in range(AGGREGATE_BURST):
        assert gate.admit(reliable=True, progress=False, now_ms=0) is GateVerdict.ACCEPT

    assert gate.admit(reliable=True, progress=False, now_ms=0) is GateVerdict.REJECT
    assert gate.admit(reliable=False, progress=False, now_ms=0) is GateVerdict.DROP
    assert gate.rejected == 1
    assert gate.dropped == 1


def test_progress_is_limited_to_one_per_250_ms() -> None:
    gate = ClientMessageGate(now_ms=0)

    first = gate.admit(reliable=False, progress=True, now_ms=0)
    early = gate.admit(reliable=False, progress=True, now_ms=PROGRESS_INTERVAL_MS - 1)
    on_time = gate.admit(reliable=False, progress=True, now_ms=PROGRESS_INTERVAL_MS)

    assert (first, early, on_time) == (GateVerdict.ACCEPT, GateVerdict.DROP, GateVerdict.ACCEPT)
    assert gate.progress_dropped == 1


def test_dropped_progress_does_not_consume_aggregate_tokens() -> None:
    gate = ClientMessageGate(now_ms=0)
    for _ in range(10):
        gate.admit(reliable=False, progress=True, now_ms=0)

    accepted = sum(
        gate.admit(reliable=True, progress=False, now_ms=0) is GateVerdict.ACCEPT
        for _ in range(AGGREGATE_BURST)
    )

    assert accepted == AGGREGATE_BURST - 1


def test_sustained_abuse_blocks_further_client_events() -> None:
    gate = ClientMessageGate(now_ms=0)
    verdicts = [
        gate.admit(reliable=True, progress=False, now_ms=0)
        for _ in range(AGGREGATE_BURST + SUSTAINED_ABUSE_LIMIT + 5)
    ]

    assert gate.blocked
    assert verdicts[-1] is GateVerdict.BLOCKED
    # Blocking is permanent for the session and keeps counters bounded.
    assert gate.admit(reliable=True, progress=False, now_ms=10_000_000) is GateVerdict.BLOCKED
    assert gate.rejected == SUSTAINED_ABUSE_LIMIT
