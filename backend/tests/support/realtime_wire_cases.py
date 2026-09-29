"""Deterministic golden cases for the ``va.*.v1`` wire contract (shared with the frontend).

Agent-to-browser envelopes are produced by the real worker code
(:class:`MediaCheck` + ``encode_agent_message``) with a manual clock and
sequential IDs, so the fixture is exactly what the worker sends.
Browser-to-agent envelopes use fixed placeholder IDs/timestamps in the shape
the browser encoder emits. Nothing here is secret.

Regenerate after an intentional contract change::

    uv run python tests/support/realtime_wire_cases.py --write
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

from voice_agent.agent_worker.media_check import MediaCheck
from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.realtime_wire import (
    CLIENT_TOPIC,
    CONTROL_TOPIC,
    EndRequestedSignal,
    encode_agent_message,
    encode_end_requested,
)
from voice_agent.contracts.transport import (
    ClientEvent,
    PlaybackFrame,
    RealtimeTopic,
    TransportEvent,
    TransportUsage,
)
from voice_agent.events_and_latency.clock import ManualClock, SequentialIdGenerator

FIXTURE_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "realtime_wire"
SESSION_ID: Final = "00000000-0000-4000-8000-000000000001"
BROWSER_EVENT_ID: Final = "00000000-0000-4000-8000-0000000000e1"
SEGMENT_ID: Final = "00000000-0000-4000-8000-0000000000a1"
OCCURRED_AT: Final = "2026-09-29T12:00:00.000Z"
CORRELATION_ID: Final = "corr-golden"
ACK_FIELDS: Final = ("worker_generation", "cancellation_generation", "segment_id")


class _Recorder:
    """Minimal ``SessionTransportPort`` that records what the worker would send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bool]] = []

    @property
    def browser_present(self) -> bool:
        return True

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        body = json.loads(encode_agent_message(envelope, reliable=reliable))
        self.sent.append((topic.value, body, reliable))

    async def publish_audio(self, frame: PlaybackFrame) -> None:
        return None

    async def finish_segment(self, identity: PlaybackAckIdentity) -> None:
        return None

    async def wait_for_playout(self) -> None:
        return None

    async def clear_playback(self) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _never(self) -> AsyncIterator[Any]:
        if False:  # pragma: no cover - empty async iterator
            yield None

    def audio_frames(self) -> AsyncIterator[AudioFrame]:
        return self._never()

    def client_events(self) -> AsyncIterator[ClientEvent]:
        return self._never()

    def lifecycle_events(self) -> AsyncIterator[TransportEvent]:
        return self._never()

    def usage(self) -> TransportUsage:
        return TransportUsage()


def _case(name: str, topic: str, body: dict[str, Any], reliable: bool) -> dict[str, Any]:
    payload = body["payload"]
    return {
        "name": name,
        "topic": topic,
        "reliable": reliable,
        "envelope": body,
        "expected": {"event_type": body["event_type"], **payload},
    }


def _envelope(event_type: EventType, payload: dict[str, Any], event_id: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=ManualClock().utc_now(),
        session_id=SESSION_ID,
        correlation_id=CORRELATION_ID,
        component="worker",
        producer_service="agent_worker",
        visibility=EventVisibility.BROWSER_SAFE,
        payload=payload,
    )


async def _media_check_cases() -> list[dict[str, Any]]:
    recorder = _Recorder()
    check = MediaCheck(
        recorder,  # type: ignore[arg-type]
        session_id=SESSION_ID,
        correlation_id=CORRELATION_ID,
        worker_generation=1,
        clock=ManualClock(),
        ids=SequentialIdGenerator(start=1),
        lease_hint=lambda: 9000,
    )
    await check.send_state("listening")
    await check.burst()
    names = [
        "state_listening",
        "state_speaking",
        "playback_started",
        "playback_completed",
        "state_listening_after_playback",
    ]
    cases = [_case(n, t, b, r) for n, (t, b, r) in zip(names, recorder.sent, strict=True)]
    check.mic_frames, check.interval_peak, check.acks = 150, 0.25, {"started": 1}
    metrics = check._envelope(EventType.TRANSPORT_QUALITY_UPDATED, check.metrics_payload())
    await recorder.send_event(RealtimeTopic.METRICS, metrics, reliable=False)
    topic, body, reliable = recorder.sent[-1]
    return [*cases, _case("metrics", topic, body, reliable)]


def _encoded(topic: RealtimeTopic, envelope: EventEnvelope, reliable: bool) -> dict[str, Any]:
    body = json.loads(encode_agent_message(envelope, reliable=reliable))
    return {"topic": topic.value, "body": body, "reliable": reliable}


def _text_and_error_cases() -> list[dict[str, Any]]:
    rows = [
        (
            "transcript_partial",
            RealtimeTopic.TRANSCRIPT,
            _envelope(
                EventType.STT_PARTIAL,
                {"text": "hello the", "is_final": False},
                "00000000-0000-4000-8000-0000000000b1",
            ),
            False,
        ),
        (
            "transcript_final",
            RealtimeTopic.TRANSCRIPT,
            _envelope(
                EventType.STT_FINAL,
                {"text": "hello there", "is_final": True},
                "00000000-0000-4000-8000-0000000000b2",
            ),
            True,
        ),
        (
            "response_final",
            RealtimeTopic.RESPONSE,
            _envelope(
                EventType.CONVERSATION_COMPLETED,
                {"text": "Hi! How can I help?", "is_final": True},
                "00000000-0000-4000-8000-0000000000b3",
            ),
            True,
        ),
        (
            "error_transport",
            RealtimeTopic.ERROR,
            _envelope(
                EventType.ERROR_UNRECOVERABLE,
                {
                    "code": "transport_error",
                    "message": "The connection was lost.",
                    "retryable": False,
                },
                "00000000-0000-4000-8000-0000000000b4",
            ),
            True,
        ),
    ]
    cases = []
    for name, topic, envelope, reliable in rows:
        encoded = _encoded(topic, envelope, reliable)
        cases.append(_case(name, encoded["topic"], encoded["body"], reliable))
    return cases


def _control_case() -> dict[str, Any]:
    signal = EndRequestedSignal.build(
        event_id="00000000-0000-4000-8000-0000000000c1",
        session_id=SESSION_ID,
        correlation_id=CORRELATION_ID,
        occurred_at=ManualClock().utc_now(),
        termination_request_revision=1,
        reason=DisconnectReason.USER_ENDED,
    )
    body = json.loads(encode_end_requested(signal))
    case = _case("control_end_requested", CONTROL_TOPIC, body, True)
    # Server -> agent only (targeted at the agent identity); browsers never receive it.
    case["direction"] = "server_to_agent"
    return case


def agent_to_browser_cases() -> list[dict[str, Any]]:
    return [*asyncio.run(_media_check_cases()), *_text_and_error_cases(), _control_case()]


def _browser(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": BROWSER_EVENT_ID,
        "session_id": SESSION_ID,
        "event_type": event_type,
        "occurred_at": OCCURRED_AT,
        "payload": payload,
    }


_ACK = {"worker_generation": 1, "cancellation_generation": 0, "segment_id": SEGMENT_ID}
_BROWSER_ROWS: Final[tuple[tuple[str, str, dict[str, Any], bool], ...]] = (
    ("client_ready", "client.ready", {}, True),
    ("mic_muted", "client.mic_muted", {}, True),
    ("mic_unmuted", "client.mic_unmuted", {}, True),
    ("playback_started", "playback.started", dict(_ACK), True),
    ("playback_progress", "playback.progress", {**_ACK, "position_ms": 240}, False),
    ("playback_completed", "playback.completed", dict(_ACK), True),
    ("playback_failed", "playback.failed", dict(_ACK), True),
)


def browser_to_agent_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "topic": CLIENT_TOPIC,
            "event_type": event_type,
            "reliable": reliable,
            "payload": payload,
            "envelope": _browser(event_type, payload),
        }
        for name, event_type, payload, reliable in _BROWSER_ROWS
    ]


def browser_to_agent_invalid_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "unknown_event_type",
            "topic": CLIENT_TOPIC,
            "envelope": _browser("client.exfiltrate", {}),
            "reason": "unknown_event_type",
        },
        {
            "name": "unsupported_schema_version",
            "topic": CLIENT_TOPIC,
            "envelope": {**_browser("client.ready", {}), "schema_version": 2},
            "reason": "unsupported_schema_version",
        },
        {
            "name": "progress_without_position",
            "topic": CLIENT_TOPIC,
            "envelope": _browser("playback.progress", dict(_ACK)),
            "reason": "invalid_message",
        },
        {
            "name": "restricted_payload_field",
            "topic": CLIENT_TOPIC,
            "envelope": _browser("client.ready", {"transcript": "x"}),
            "reason": "invalid_message",
        },
    ]


FILES: Final = {
    "agent_to_browser.json": agent_to_browser_cases,
    "browser_to_agent.json": browser_to_agent_cases,
    "browser_to_agent_invalid.json": browser_to_agent_invalid_cases,
}


def render(cases: list[dict[str, Any]]) -> str:
    return json.dumps(cases, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":  # pragma: no cover - fixture regeneration
    if "--write" in sys.argv:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for filename, build in FILES.items():
            (FIXTURE_DIR / filename).write_text(render(build()), encoding="utf-8", newline="\n")
