"""Interruption-to-silence samples and the WP10 latency gate (docs/14 §16, docs/17 §19A).

Each accepted interruption stores, on the interrupted turn
(``interruption_summary``) and on its durable ``turn.interrupted`` event:
``accepted_at``, ``playback_stopped_at`` (when ``AudioSource.clear_queue()``
returned), and ``interruption_latency_ms``. This module turns those stored
records into samples and evaluates the gate: at least 20 valid samples,
P95 at most 500 ms, maximum at most 1,000 ms.

A sample is valid only for a *user barge-in over audible agent speech*
(phase ``speaking``) with a recorded latency. Every other interruption is
kept with its exclusion cause, never silently dropped (docs/17 §19A). The
P95 is the nearest-rank percentile over valid samples; no percentile is
reported from fewer than the minimum sample count.

The server-side value measures acceptance to the moment the worker's audio
queue was cleared; audio already handed to WebRTC (network and browser
jitter buffer) is outside it. The ``INT-LIVE`` harness adds that residual
from browser-side observation before applying the gate.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from voice_agent.contracts.enums import InterruptionPhase, InterruptionReason
from voice_agent.domain.turn import ConversationTurn

MINIMUM_VALID_SAMPLES: Final = 20
P95_LIMIT_MS: Final = 500
MAXIMUM_LIMIT_MS: Final = 1_000
P95: Final = 0.95


class SampleExclusion(StrEnum):
    NOT_BARGE_IN = "not_user_barge_in"
    NOT_AUDIBLE = "agent_audio_not_audible"
    NO_TIMING = "timing_unavailable"


@dataclass(frozen=True, slots=True)
class InterruptionSample:
    turn_id: str
    latency_ms: int | None
    exclusion: SampleExclusion | None

    @property
    def is_valid(self) -> bool:
        return self.exclusion is None and self.latency_ms is not None


@dataclass(frozen=True, slots=True)
class InterruptionLatencySummary:
    valid_count: int
    excluded: tuple[InterruptionSample, ...]
    p95_ms: int | None
    maximum_ms: int | None
    gate_met: bool


def sample_of(turn: ConversationTurn) -> InterruptionSample | None:
    """The sample for one interrupted turn, or ``None`` when nothing was accepted."""
    summary = turn.interruption
    if not summary.accepted:
        return None
    exclusion: SampleExclusion | None = None
    if summary.reason is not InterruptionReason.USER_BARGE_IN:
        exclusion = SampleExclusion.NOT_BARGE_IN
    elif summary.phase is not InterruptionPhase.SPEAKING:
        exclusion = SampleExclusion.NOT_AUDIBLE
    elif summary.interruption_latency_ms is None:
        exclusion = SampleExclusion.NO_TIMING
    return InterruptionSample(turn.turn_id, summary.interruption_latency_ms, exclusion)


def samples_from_turns(turns: Iterable[ConversationTurn]) -> list[InterruptionSample]:
    return [sample for turn in turns if (sample := sample_of(turn)) is not None]


def nearest_rank(values: list[int], quantile: float) -> int:
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    rank = max(math.ceil(quantile * len(ordered)), 1)
    return ordered[rank - 1]


def summarize(
    samples: Iterable[InterruptionSample],
    *,
    minimum: int = MINIMUM_VALID_SAMPLES,
    p95_limit_ms: int = P95_LIMIT_MS,
    maximum_limit_ms: int = MAXIMUM_LIMIT_MS,
) -> InterruptionLatencySummary:
    collected = list(samples)
    valid = [s.latency_ms for s in collected if s.is_valid and s.latency_ms is not None]
    excluded = tuple(s for s in collected if not s.is_valid)
    if len(valid) < minimum:
        return InterruptionLatencySummary(len(valid), excluded, None, None, gate_met=False)
    p95, maximum = nearest_rank(valid, P95), max(valid)
    met = p95 <= p95_limit_ms and maximum <= maximum_limit_ms
    return InterruptionLatencySummary(len(valid), excluded, p95, maximum, gate_met=met)
