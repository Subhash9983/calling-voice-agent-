"""The worker records the browser's per-turn ``client.latency_sample`` (docs/06 §15).

``client.latency_sample`` itself is non-durable (docs/01 §9, docs/02 §9);
the worker keeps its bounded per-turn aggregate as a durable
``transport.quality_updated`` event so the composed docs/11 §11 latency can
be rebuilt from stored evidence. Raw WebRTC stats are never stored.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.support.conversation_rig import Rig, build
from tests.support.fake_openai import reply

from voice_agent.contracts.events import EventType
from voice_agent.contracts.transport import ClientLatencySample
from voice_agent.events_and_latency.first_audible import (
    FIRST_FRAME_AT_KEY,
    FirstAudibleMethod,
    first_audible_sample,
    latency_sample_payload,
)

pytestmark = pytest.mark.asyncio
UNKNOWN_TURN = "00000000-0000-4000-8000-00000000dead"


async def _one_turn() -> tuple[Rig, str]:
    rig = build([reply("Namaste! ", "Kaise hain?")], ["Namaste."])
    await rig.start()
    await rig.utterance()
    await rig.idle()
    [turn] = await rig.all_turns()
    return rig, turn.turn_id


def _reports(rig: Rig) -> list[dict[str, object]]:
    return [r.envelope.payload for r in rig.events.of(EventType.TRANSPORT_QUALITY_UPDATED)]


async def test_a_browser_latency_sample_becomes_durable_turn_evidence() -> None:
    rig, turn_id = await _one_turn()

    rig.transport.client(
        ClientLatencySample(turn_id=turn_id, browser_playout_ms=85, network_one_way_ms=22)
    )
    await rig.until(lambda: bool(_reports(rig)))
    await rig.stop()

    [record] = rig.events.of(EventType.TRANSPORT_QUALITY_UPDATED)
    assert record.envelope.turn_id == turn_id
    assert record.envelope.payload == latency_sample_payload(85, 22)
    ordered = sorted(
        (r.envelope for r in rig.events.records if r.envelope.turn_id == turn_id),
        key=lambda e: e.occurred_at,
    )
    started = next(e for e in ordered if e.event_type is EventType.PLAYBACK_STARTED)
    assert isinstance(started.payload[FIRST_FRAME_AT_KEY], int)
    sample = first_audible_sample(turn_id, ordered)
    assert sample is not None
    assert sample.method is FirstAudibleMethod.COMPOSED
    assert (sample.browser_playout_ms, sample.network_one_way_ms) == (85, 22)


async def test_unknown_turns_and_repeats_are_never_recorded() -> None:
    rig, turn_id = await _one_turn()

    rig.transport.client(ClientLatencySample(turn_id=UNKNOWN_TURN, browser_playout_ms=40))
    rig.transport.client(ClientLatencySample(turn_id=turn_id, browser_playout_ms=70))
    rig.transport.client(ClientLatencySample(turn_id=turn_id, browser_playout_ms=999))
    await rig.until(lambda: bool(_reports(rig)))
    await asyncio.sleep(0.05)
    await rig.stop()

    assert _reports(rig) == [latency_sample_payload(70, None)]
    counters = rig.orchestrator.counters
    assert counters["latency_samples_recorded"] == 1
    assert counters["latency_samples_ignored"] == 2
