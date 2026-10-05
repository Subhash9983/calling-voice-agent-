"""Golden ``va.*.v1`` fixtures shared with the browser (``tests/fixtures/realtime_wire``).

- the worker's encoder reproduces every ``agent_to_browser.json`` envelope
  byte-for-byte in content, and each envelope carries its ``expected`` fields;
- the decoder accepts every ``browser_to_agent.json`` envelope and rejects the
  named ``browser_to_agent_invalid.json`` cases with their reason code;
- the fixtures stay deterministic and free of secrets/internal fields.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.support.realtime_wire_cases import FILES, FIXTURE_DIR, render

from voice_agent.contracts.realtime_wire import (
    MAX_LOSSY_PAYLOAD_BYTES,
    MAX_RELIABLE_PAYLOAD_BYTES,
    ClientEventType,
    WireRejectedError,
    decode_client_message,
    decode_end_requested,
)
from voice_agent.contracts.transport import (
    ClientMicState,
    ClientReady,
    PlaybackAck,
    PlaybackAckKind,
)

REGENERATE = "uv run python tests/support/realtime_wire_cases.py --write"
FORBIDDEN_ENVELOPE_KEYS = {"correlation_id", "producer_service", "component", "provider", "model"}


def _load(filename: str) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads((FIXTURE_DIR / filename).read_text("utf-8"))
    return loaded


@pytest.mark.parametrize("filename", sorted(FILES))
def test_fixture_file_is_what_the_code_produces(filename: str) -> None:
    on_disk = (FIXTURE_DIR / filename).read_text("utf-8")

    assert on_disk == render(FILES[filename]()), f"fixture drifted; regenerate with: {REGENERATE}"


AGENT_CASES = _load("agent_to_browser.json")
BROWSER_CASES = _load("browser_to_agent.json")
INVALID_CASES = _load("browser_to_agent_invalid.json")


def test_every_agent_topic_is_covered() -> None:
    topics = {case["topic"] for case in AGENT_CASES}
    playback = {
        case["expected"].get("state") for case in AGENT_CASES if "playback" in case["topic"]
    }

    assert topics == {
        "va.state.v1",
        "va.transcript.v1",
        "va.response.v1",
        "va.playback.v1",
        "va.error.v1",
        "va.metrics.v1",
        "va.control.v1",
    }
    assert playback == {"started", "completed", "cancelled"}


@pytest.mark.parametrize("case", AGENT_CASES, ids=lambda c: c["name"])
def test_agent_envelopes_are_bounded_browser_safe_and_match_expected(case: dict[str, Any]) -> None:
    envelope = case["envelope"]
    encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    limit = MAX_RELIABLE_PAYLOAD_BYTES if case["reliable"] else MAX_LOSSY_PAYLOAD_BYTES
    expected = dict(case["expected"])

    assert len(encoded) <= limit
    assert envelope["schema_version"] == 1
    assert envelope["event_type"] == expected.pop("event_type")
    assert expected.items() <= envelope["payload"].items()
    if case["topic"] == "va.control.v1":
        assert case["direction"] == "server_to_agent"
        assert decode_end_requested(encoded).session_id == envelope["session_id"]
    else:
        assert not FORBIDDEN_ENVELOPE_KEYS & set(envelope)
    if case["topic"] == "va.playback.v1":
        assert {"worker_generation", "cancellation_generation", "segment_id"} <= set(expected)


_DECODED: dict[str, Any] = {
    ClientEventType.READY.value: ClientReady(),
    ClientEventType.MIC_MUTED.value: ClientMicState(muted=True),
    ClientEventType.MIC_UNMUTED.value: ClientMicState(muted=False),
}
_ACKS = {
    "playback.started": PlaybackAckKind.STARTED,
    "playback.progress": PlaybackAckKind.PROGRESS,
    "playback.completed": PlaybackAckKind.COMPLETED,
    "playback.failed": PlaybackAckKind.FAILED,
}


@pytest.mark.parametrize("case", BROWSER_CASES, ids=lambda c: c["name"])
def test_browser_envelopes_decode(case: dict[str, Any]) -> None:
    raw = json.dumps(case["envelope"]).encode("utf-8")

    decoded = decode_client_message(raw, reliable=case["reliable"])

    assert decoded.event_type.value == case["event_type"]
    assert decoded.session_id == case["envelope"]["session_id"]
    if case["event_type"] in _ACKS:
        assert isinstance(decoded.event, PlaybackAck)
        assert decoded.event.ack is _ACKS[case["event_type"]]
        assert decoded.event.identity.segment_id == case["payload"]["segment_id"]
    else:
        assert decoded.event == _DECODED[case["event_type"]]


def test_every_browser_event_is_covered() -> None:
    names = {case["event_type"] for case in BROWSER_CASES}

    assert names == {
        "client.ready",
        "client.mic_muted",
        "client.mic_unmuted",
        "playback.started",
        "playback.progress",
        "playback.completed",
        "playback.failed",
    }


@pytest.mark.parametrize("case", INVALID_CASES, ids=lambda c: c["name"])
def test_invalid_browser_envelopes_are_rejected(case: dict[str, Any]) -> None:
    raw = json.dumps(case["envelope"]).encode("utf-8")

    with pytest.raises(WireRejectedError) as caught:
        decode_client_message(raw, reliable=True)

    assert caught.value.reason.value == case["reason"]


def test_fixtures_contain_no_secret_like_values() -> None:
    text = "".join((FIXTURE_DIR / name).read_text("utf-8") for name in FILES)

    for marker in ("wss://", "mongodb", "eyJ", "api_key", "secret", "token"):
        assert marker not in text
