"""Composed speech-end -> first-audible latency (docs/11 §11, docs/06 §15, Decision 067 S11).

The end-to-end sample is worker span (worker monotonic clock) + browser
playout span + one-way network estimate, never a cross-machine wall-clock
subtraction. Each sample records how its network component was obtained
and that estimate's uncertainty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.events_and_latency.first_audible import (
    ASSUMED_NETWORK_ONE_WAY_MS,
    ASSUMED_NETWORK_UNCERTAINTY_MS,
    BROWSER_PLAYOUT_KEY,
    FIRST_FRAME_AT_KEY,
    LATENCY_SAMPLE_AGGREGATE,
    NETWORK_ONE_WAY_KEY,
    FirstAudibleMethod,
    FirstAudibleSample,
    compose,
    first_audible_sample,
    latency_sample_payload,
)
from voice_agent.events_and_latency.latency import first_audible_breakdown

SESSION = "00000000-0000-4000-8000-0000000000a1"
TURN = "00000000-0000-4000-8000-0000000000b1"
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _event(event_type: EventType, at_ms: int, **payload: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"00000000-0000-4000-8000-{at_ms + 1:012x}",
        event_type=event_type,
        occurred_at=T0 + timedelta(milliseconds=at_ms),
        session_id=SESSION,
        turn_id=TURN,
        correlation_id="wp12",
        component="worker",
        producer_service="agent_worker",
        payload=payload,
    )


def _worker_events(*, last_speech: int = 10_000, first_frame: int = 11_400) -> list[EventEnvelope]:
    return [
        _event(EventType.USER_SPEECH_ENDED, 700, last_speech_at_ms=last_speech),
        # The wall-clock occurred_at values are deliberately inconsistent with
        # the monotonic span: the composition must not use them.
        _event(EventType.PLAYBACK_STARTED, 5_000, **{FIRST_FRAME_AT_KEY: first_frame}),
        _event(EventType.PLAYBACK_STARTED, 6_000, **{FIRST_FRAME_AT_KEY: first_frame + 900}),
    ]


def _browser(playout: int, network: int | None = None) -> EventEnvelope:
    return _event(
        EventType.TRANSPORT_QUALITY_UPDATED, 7_000, **latency_sample_payload(playout, network)
    )


def test_a_complete_sample_adds_worker_browser_and_measured_network() -> None:
    sample = first_audible_sample(TURN, [*_worker_events(), _browser(120, 40)])

    assert sample == FirstAudibleSample(
        turn_id=TURN,
        worker_span_ms=1_400,
        browser_playout_ms=120,
        network_one_way_ms=40,
        network_uncertainty_ms=40,
        method=FirstAudibleMethod.COMPOSED,
    )
    assert sample.total_ms == 1_560
    assert sample.is_composed


def test_a_missing_network_estimate_uses_the_documented_fallback_and_is_flagged() -> None:
    sample = first_audible_sample(TURN, [*_worker_events(), _browser(120)])

    assert sample is not None
    assert sample.method is FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED
    assert sample.network_one_way_ms == ASSUMED_NETWORK_ONE_WAY_MS
    assert sample.network_uncertainty_ms == ASSUMED_NETWORK_UNCERTAINTY_MS
    # The missing network component is never treated as zero.
    assert sample.total_ms == 1_400 + 120 + ASSUMED_NETWORK_ONE_WAY_MS
    assert sample.is_composed


def test_without_a_browser_span_the_sample_is_worker_only_and_not_composed() -> None:
    sample = first_audible_sample(TURN, _worker_events())

    assert sample is not None
    assert sample.method is FirstAudibleMethod.WORKER_ONLY
    assert (sample.browser_playout_ms, sample.network_one_way_ms) == (None, None)
    assert sample.network_uncertainty_ms is None
    assert sample.total_ms == 1_400
    assert not sample.is_composed


@pytest.mark.parametrize(
    "events",
    [
        [_event(EventType.PLAYBACK_STARTED, 0, **{FIRST_FRAME_AT_KEY: 5})],
        [_event(EventType.USER_SPEECH_ENDED, 0, last_speech_at_ms=5)],
        # Legacy playback.started without the worker monotonic first-frame time.
        [
            _event(EventType.USER_SPEECH_ENDED, 0, last_speech_at_ms=5),
            _event(EventType.PLAYBACK_STARTED, 900),
        ],
        # First frame before the last speech frame: not a valid span.
        _worker_events(last_speech=2_000, first_frame=1_000),
        # Booleans are not integers here.
        [
            _event(EventType.USER_SPEECH_ENDED, 0, last_speech_at_ms=True),
            _event(EventType.PLAYBACK_STARTED, 0, **{FIRST_FRAME_AT_KEY: 9}),
        ],
    ],
)
def test_no_worker_span_means_no_sample_never_zero(events: list[EventEnvelope]) -> None:
    assert first_audible_sample(TURN, [*events, _browser(100, 10)]) is None


def test_only_the_turns_first_browser_report_counts_and_other_quality_events_are_ignored() -> None:
    unrelated = _event(EventType.TRANSPORT_QUALITY_UPDATED, 6_500, mic_frames=50)
    later = _event(EventType.TRANSPORT_QUALITY_UPDATED, 9_000, **latency_sample_payload(999, 999))

    sample = first_audible_sample(TURN, [*_worker_events(), unrelated, _browser(80, 20), later])

    assert sample is not None
    assert (sample.browser_playout_ms, sample.network_one_way_ms) == (80, 20)


def test_the_durable_payload_is_bounded_and_marks_the_aggregate() -> None:
    assert latency_sample_payload(80, None) == {
        "aggregate": LATENCY_SAMPLE_AGGREGATE,
        BROWSER_PLAYOUT_KEY: 80,
    }
    assert latency_sample_payload(80, 15)[NETWORK_ONE_WAY_KEY] == 15


def test_compose_rejects_negative_components() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compose(TURN, worker_span_ms=-1, browser_playout_ms=None, network_one_way_ms=None)
    with pytest.raises(ValueError, match="non-negative"):
        compose(TURN, worker_span_ms=1, browser_playout_ms=-5, network_one_way_ms=None)


def test_the_breakdown_separates_composed_from_worker_only_samples() -> None:
    complete = [
        compose(TURN, worker_span_ms=1_000 + i, browser_playout_ms=100, network_one_way_ms=30)
        for i in range(3)
    ]
    assumed = [compose(TURN, worker_span_ms=1_500, browser_playout_ms=90, network_one_way_ms=None)]
    worker_only = [
        compose(TURN, worker_span_ms=900, browser_playout_ms=None, network_one_way_ms=None)
    ]

    breakdown = first_audible_breakdown([*complete, *assumed, *worker_only])

    assert breakdown["sample_counts"] == {
        "composed": 3,
        "composed_network_assumed": 1,
        "worker_only": 1,
    }
    composed = breakdown["composed_all"]
    assert composed["sample_count"] == 4
    assert composed["p50_ms"] == 1_131  # nearest rank over 1130,1131,1132,1690
    assert composed["p95_ms"] == 1_500 + 90 + ASSUMED_NETWORK_ONE_WAY_MS
    assert breakdown["composed_measured_network"]["sample_count"] == 3
    assert breakdown["composed_assumed_network"]["sample_count"] == 1
    assert breakdown["worker_only_diagnostic"]["p50_ms"] == 900
    assert breakdown["max_network_uncertainty_ms"] == ASSUMED_NETWORK_UNCERTAINTY_MS
    assert breakdown["meets_composed_method"] is False


def test_an_empty_breakdown_has_no_statistics() -> None:
    breakdown = first_audible_breakdown([])

    assert breakdown["composed_all"] is None
    assert breakdown["worker_only_diagnostic"] is None
    assert breakdown["max_network_uncertainty_ms"] is None
    assert breakdown["meets_composed_method"] is False
