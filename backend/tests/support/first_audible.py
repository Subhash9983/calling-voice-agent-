"""Stored evidence of one turn's composed speech-end -> first-audible latency (tests only)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from tests.support.persistence_builders import make_event

from voice_agent.contracts.events import EventType
from voice_agent.domain.control_session import SessionRecord
from voice_agent.events_and_latency.first_audible import (
    FIRST_FRAME_AT_KEY,
    LAST_SPEECH_AT_KEY,
    latency_sample_payload,
)
from voice_agent.ports.control_plane import EventRecord

# Arbitrary worker monotonic origin: unrelated to the wall-clock timestamps.
MONOTONIC_ORIGIN_MS = 7_300_000


def payload_event(
    session: SessionRecord,
    event_type: EventType,
    *,
    occurred_at: datetime,
    turn_id: str,
    payload: dict[str, Any],
) -> EventRecord:
    record = make_event(session, event_type, occurred_at=occurred_at, turn_id=turn_id)
    envelope = record.envelope.model_copy(update={"payload": payload})
    return EventRecord(envelope, record.severity, record.recorded_at)


def turn_latency_events(
    session: SessionRecord,
    turn_id: str,
    *,
    start: datetime,
    worker_span_ms: int,
    browser_playout_ms: int | None = None,
    network_one_way_ms: int | None = None,
) -> list[EventRecord]:
    """``user.speech_ended`` + first ``playback.started`` (+ the browser report when given)."""
    last_speech = MONOTONIC_ORIGIN_MS
    events = [
        payload_event(
            session,
            EventType.USER_SPEECH_ENDED,
            occurred_at=start,
            turn_id=turn_id,
            payload={LAST_SPEECH_AT_KEY: last_speech, "committed_at_ms": last_speech + 700},
        ),
        payload_event(
            session,
            EventType.PLAYBACK_STARTED,
            # Wall clock deliberately unrelated to the monotonic span.
            occurred_at=start + timedelta(milliseconds=worker_span_ms // 2),
            turn_id=turn_id,
            payload={FIRST_FRAME_AT_KEY: last_speech + worker_span_ms, "piece_index": 0},
        ),
    ]
    if browser_playout_ms is not None:
        events.append(
            payload_event(
                session,
                EventType.TRANSPORT_QUALITY_UPDATED,
                occurred_at=start + timedelta(milliseconds=worker_span_ms + 50),
                turn_id=turn_id,
                payload=latency_sample_payload(browser_playout_ms, network_one_way_ms),
            )
        )
    return events
