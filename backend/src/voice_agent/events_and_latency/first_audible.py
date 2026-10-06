"""Composed speech-end -> first-audible latency (docs/11 §11; docs/06 §15; Decision 067 S11).

The worker and the browser run on different clocks, so the end-to-end value
is *composed* and never computed by subtracting wall clocks across machines:

1. worker span: worker monotonic time of the first TTS frame handed to the
   ``AudioSource`` (``playback.started`` payload ``first_frame_at_ms``) minus
   the worker monotonic time of the committed turn's last VAD speech frame
   (``user.speech_ended`` payload ``last_speech_at_ms``). Both values are on
   the same worker process's monotonic timeline;
2. browser span: first agent audio packet received -> playout, from WebRTC
   receiver stats, reported once per turn by the browser as the non-durable
   ``client.latency_sample`` and recorded by the worker as a bounded
   per-turn transport aggregate (``transport.quality_updated`` with
   ``aggregate = "turn_latency_sample"``);
3. one-way network estimate: RTT/2 from WebRTC stats, when the browser
   reported one.

Uncertainty of the network estimate (recorded on every composed sample):

- measured (``composed``): the true one-way delay of an RTT/2 estimate lies
  anywhere in ``[0, RTT]`` for an asymmetric path, so the uncertainty is
  ``±RTT/2`` (= the estimate itself);
- missing (``composed_network_assumed``): the documented fallback is the
  Decision 066 transport/browser diagnostic stage budget — 100 ms (P50) as
  the estimate with ±250 ms uncertainty (up to the 350 ms P95 budget). The
  missing component is never treated as zero;
- no browser span at all (``worker_only``): the sample does not meet the
  composed method. It is a diagnostic lower bound only, never gate evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from pydantic import JsonValue

from voice_agent.contracts.events import EventEnvelope, EventType

FIRST_FRAME_AT_KEY: Final = "first_frame_at_ms"
LAST_SPEECH_AT_KEY: Final = "last_speech_at_ms"
BROWSER_PLAYOUT_KEY: Final = "browser_playout_ms"
NETWORK_ONE_WAY_KEY: Final = "network_one_way_ms"
AGGREGATE_KEY: Final = "aggregate"
LATENCY_SAMPLE_AGGREGATE: Final = "turn_latency_sample"
# Decision 066 transport/browser diagnostic stage budget: 100 ms P50, 350 ms P95.
ASSUMED_NETWORK_ONE_WAY_MS: Final = 100
ASSUMED_NETWORK_UNCERTAINTY_MS: Final = 250


class FirstAudibleMethod(StrEnum):
    """How a speech-end -> first-audible sample was obtained."""

    COMPOSED = "composed"
    COMPOSED_NETWORK_ASSUMED = "composed_network_assumed"
    WORKER_ONLY = "worker_only"


@dataclass(frozen=True, slots=True)
class FirstAudibleSample:
    turn_id: str
    worker_span_ms: int
    browser_playout_ms: int | None
    network_one_way_ms: int | None
    network_uncertainty_ms: int | None
    method: FirstAudibleMethod

    @property
    def is_composed(self) -> bool:
        """Only composed samples meet the docs/11 §11 method (gate evidence)."""
        return self.method is not FirstAudibleMethod.WORKER_ONLY

    @property
    def total_ms(self) -> int:
        """Composed total; for ``worker_only`` this is the worker span (a lower bound)."""
        return self.worker_span_ms + (self.browser_playout_ms or 0) + (self.network_one_way_ms or 0)


class MeasuredFirstAudible(Protocol):
    """Anything that carries one sample's method, total, and network uncertainty."""

    @property
    def method(self) -> FirstAudibleMethod: ...

    @property
    def total_ms(self) -> int: ...

    @property
    def network_uncertainty_ms(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class RecordedFirstAudible:
    """A stored sample (e.g. an evaluation result's measurements)."""

    method: FirstAudibleMethod
    total_ms: int
    network_uncertainty_ms: int | None


def compose(
    turn_id: str,
    *,
    worker_span_ms: int,
    browser_playout_ms: int | None,
    network_one_way_ms: int | None,
) -> FirstAudibleSample:
    """Build one sample, choosing the method from which components are present."""
    parts = (worker_span_ms, browser_playout_ms, network_one_way_ms)
    if any(part is not None and part < 0 for part in parts):
        raise ValueError("latency components must be non-negative")
    if browser_playout_ms is None:
        return FirstAudibleSample(
            turn_id, worker_span_ms, None, None, None, FirstAudibleMethod.WORKER_ONLY
        )
    if network_one_way_ms is None:
        return FirstAudibleSample(
            turn_id,
            worker_span_ms,
            browser_playout_ms,
            ASSUMED_NETWORK_ONE_WAY_MS,
            ASSUMED_NETWORK_UNCERTAINTY_MS,
            FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED,
        )
    return FirstAudibleSample(
        turn_id,
        worker_span_ms,
        browser_playout_ms,
        network_one_way_ms,
        network_one_way_ms,
        FirstAudibleMethod.COMPOSED,
    )


def latency_sample_payload(
    browser_playout_ms: int, network_one_way_ms: int | None
) -> dict[str, JsonValue]:
    """Bounded durable payload of one browser-reported per-turn latency aggregate."""
    payload: dict[str, JsonValue] = {
        AGGREGATE_KEY: LATENCY_SAMPLE_AGGREGATE,
        BROWSER_PLAYOUT_KEY: browser_playout_ms,
    }
    if network_one_way_ms is not None:
        payload[NETWORK_ONE_WAY_KEY] = network_one_way_ms
    return payload


def _ms(event: EventEnvelope | None, key: str) -> int | None:
    value = None if event is None else event.payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first(events: Iterable[EventEnvelope], event_type: EventType) -> EventEnvelope | None:
    return next((e for e in events if e.event_type is event_type), None)


def _browser_report(events: Iterable[EventEnvelope]) -> EventEnvelope | None:
    return next(
        (
            e
            for e in events
            if e.event_type is EventType.TRANSPORT_QUALITY_UPDATED
            and e.payload.get(AGGREGATE_KEY) == LATENCY_SAMPLE_AGGREGATE
            and _ms(e, BROWSER_PLAYOUT_KEY) is not None
        ),
        None,
    )


def worker_span_ms(events: Iterable[EventEnvelope]) -> int | None:
    """Last VAD speech frame -> first TTS frame written, on the worker monotonic clock."""
    ordered = list(events)
    last_speech = _ms(_first(ordered, EventType.USER_SPEECH_ENDED), LAST_SPEECH_AT_KEY)
    first_frame = _ms(_first(ordered, EventType.PLAYBACK_STARTED), FIRST_FRAME_AT_KEY)
    if last_speech is None or first_frame is None or first_frame < last_speech:
        return None
    return first_frame - last_speech


def first_audible_sample(
    turn_id: str, ordered_events: Iterable[EventEnvelope]
) -> FirstAudibleSample | None:
    """One turn's sample from its own durable events (already ordered and turn-filtered)."""
    events = list(ordered_events)
    span = worker_span_ms(events)
    if span is None:
        return None
    report = _browser_report(events)
    return compose(
        turn_id,
        worker_span_ms=span,
        browser_playout_ms=_ms(report, BROWSER_PLAYOUT_KEY),
        network_one_way_ms=_ms(report, NETWORK_ONE_WAY_KEY),
    )
