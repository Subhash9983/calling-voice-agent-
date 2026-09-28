"""Event envelope: vocabulary, versioning, durable subset, restricted fields.

docs/01 §8-§10, §23; docs/02 §9.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent.contracts.events import (
    DURABLE_EVENT_TYPES,
    EVENT_SCHEMA_VERSION,
    TERMINAL_EVENT_TYPES,
    EventCategory,
    EventEnvelope,
    EventType,
    EventVisibility,
    browser_safe_view,
    durable_category,
    parse_event,
)

SESSION = "00000000-0000-4000-8000-000000000001"
EVENT = "00000000-0000-4000-8000-000000000002"


def _envelope(**overrides: Any) -> EventEnvelope:
    values: dict[str, Any] = {
        "event_id": EVENT,
        "event_type": EventType.TURN_OPENED,
        "occurred_at": datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
        "session_id": SESSION,
        "correlation_id": "corr-1",
        "component": "orchestrator",
        "producer_service": "agent_worker",
    }
    values.update(overrides)
    return EventEnvelope(**values)


def test_catalogue_matches_docs_01_section_9() -> None:
    # 12 session/transport + 10 speech/STT + 8 conversation + 14 TTS/playback + 5 client
    # + 1 agent + 5 worker + 8 turn + 3 usage/cost + 10 consent + 3 error
    assert len(EventType) == 79
    assert all(event.value == event.value.lower() for event in EventType)
    assert EventType("worker.self_fenced") is EventType.WORKER_SELF_FENCED
    assert EventType("agent.recovering") is EventType.AGENT_RECOVERING


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.STT_PARTIAL,
        EventType.CONVERSATION_TEXT_DELTA,
        EventType.TTS_AUDIO_FRAME,
        EventType.PLAYBACK_PROGRESS,
        EventType.PLAYBACK_FAILED,
        EventType.CLIENT_READY,
        EventType.CLIENT_MIC_MUTED,
        EventType.CLIENT_LATENCY_SAMPLE,
        EventType.AGENT_RECOVERING,
    ],
)
def test_realtime_only_events_are_never_durable(event_type: EventType) -> None:
    assert event_type not in DURABLE_EVENT_TYPES
    assert durable_category(event_type) is None


@pytest.mark.parametrize(
    ("event_type", "category"),
    [
        (EventType.SESSION_END_REQUESTED, EventCategory.SESSION),
        (EventType.USER_SPEECH_STARTED, EventCategory.SPEECH),
        (EventType.WORKER_RECOVERY_CLAIMED, EventCategory.WORKER),
        (EventType.RECORDING_STARTED, EventCategory.CONSENT),
        (EventType.COST_CALCULATED, EventCategory.COST),
        (EventType.ERROR_UNRECOVERABLE, EventCategory.ERROR),
        (EventType.TURN_FALSE_INTERRUPTION_SUPPRESSED, EventCategory.TURN),
    ],
)
def test_durable_events_map_to_one_persistence_category(
    event_type: EventType, category: EventCategory
) -> None:
    assert durable_category(event_type) is category


def test_terminal_events_are_durable_session_and_turn_endings() -> None:
    assert TERMINAL_EVENT_TYPES <= DURABLE_EVENT_TYPES
    assert _envelope(event_type=EventType.TURN_DISCARDED).is_terminal
    assert not _envelope(event_type=EventType.TURN_OPENED).is_terminal


def test_envelope_round_trips_through_json_with_current_schema_version() -> None:
    envelope = _envelope(payload={"segment_sequence": 2, "language_code": "hi-IN"})

    restored = EventEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert json.loads(envelope.model_dump_json())["schema_version"] == EVENT_SCHEMA_VERSION


def test_unsupported_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported event schema version"):
        _envelope(schema_version=2)


def test_unknown_fields_are_rejected() -> None:
    raw = json.loads(_envelope().model_dump_json())
    raw["provider_payload_blob"] = {"x": 1}

    with pytest.raises(ValidationError, match="Extra inputs"):
        EventEnvelope.model_validate(raw)


def test_unknown_event_type_is_ignored_safely() -> None:
    raw = json.loads(_envelope().model_dump_json())
    raw["event_type"] = "future.event_type"

    assert parse_event(json.dumps(raw)) is None


def test_parse_event_still_rejects_other_violations() -> None:
    raw = json.loads(_envelope().model_dump_json())
    raw["event_type"] = "future.event_type"
    raw["session_id"] = "not-a-uuid"

    with pytest.raises(ValidationError):
        parse_event(json.dumps(raw))


def test_parse_event_accepts_a_known_event() -> None:
    envelope = _envelope()

    assert parse_event(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize(
    "payload",
    [
        {"transcript": "hello"},
        {"api_key": "x"},
        {"nested": {"Authorization": "Bearer y"}},
        {"items": [{"prompt": "system"}]},
        {"audio": "AAAA"},
        {"stack_trace": "..."},
    ],
)
def test_restricted_payload_fields_are_rejected_at_any_depth(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="restricted fields"):
        _envelope(payload=payload)


def test_payload_is_bounded_to_16_kib() -> None:
    with pytest.raises(ValidationError, match="16 KiB"):
        _envelope(payload={"note": "x" * (16 * 1024)})


def test_timestamps_must_be_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))

    with pytest.raises(ValidationError, match="UTC"):
        _envelope(occurred_at=datetime(2026, 9, 28, 10, 0, tzinfo=ist))
    with pytest.raises(ValidationError, match="UTC"):
        _envelope(occurred_at=datetime(2026, 9, 28, 10, 0))


@pytest.mark.parametrize(
    "bad_id",
    ["1234", "00000000-0000-4000-8000-00000000000G", "00000000-0000-4000-8000-00000000000A"],
)
def test_identifiers_must_be_canonical_uuid_strings(bad_id: str) -> None:
    with pytest.raises(ValidationError, match="identifier"):
        _envelope(session_id=bad_id)


def test_browser_view_excludes_provider_and_producer_internals() -> None:
    internal = _envelope(provider="openai", model="gpt-6-luna")
    safe = _envelope(visibility=EventVisibility.BROWSER_SAFE, provider="openai")

    view = browser_safe_view(safe)

    assert browser_safe_view(internal) is None
    assert view is not None
    assert set(view) == {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "session_id",
        "turn_id",
        "operation_id",
        "sequence_number",
        "payload",
    }


def test_envelope_is_immutable() -> None:
    envelope = _envelope()

    with pytest.raises(ValidationError):
        envelope.component = "other"  # type: ignore[misc]
