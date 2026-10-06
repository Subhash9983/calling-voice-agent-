"""Durable record of the browser's per-turn ``client.latency_sample`` (docs/06 §15; docs/11 §11).

``client.latency_sample`` is a non-durable browser event (docs/01 §9,
docs/02 §9). Its bounded per-turn aggregate — browser playout span and the
optional RTT/2 network estimate — is kept as one durable
``transport.quality_updated`` event on the turn, so the composed
speech-end -> first-audible latency can be rebuilt from stored evidence
(:mod:`voice_agent.events_and_latency.first_audible`). Raw WebRTC stats are
never stored.

Only turns this worker opened are accepted, at most once each; anything
else is counted and dropped. The write goes through the ordered evidence
writer, never on the audio path.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from voice_agent.agent_worker.ordered_writer import OrderedWriter
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.events import EventType
from voice_agent.contracts.transport import ClientLatencySample
from voice_agent.events_and_latency.first_audible import latency_sample_payload
from voice_agent.ports.clock import Clock

MAX_TRACKED_TURNS: Final = 64


class LatencySampleRecorder:
    def __init__(
        self,
        evidence: SttEvidence,
        writer: OrderedWriter,
        clock: Clock,
        count: Callable[[str], None],
    ) -> None:
        self._evidence = evidence
        self._writer = writer
        self._clock = clock
        self._count = count
        # turn ID -> whether its sample was already recorded (bounded, oldest evicted).
        self._turns: OrderedDict[str, bool] = OrderedDict()

    def track(self, turn_id: str) -> None:
        """A user turn this worker opened may receive one browser latency sample."""
        self._turns[turn_id] = False
        while len(self._turns) > MAX_TRACKED_TURNS:
            self._turns.popitem(last=False)

    def record(self, sample: ClientLatencySample) -> bool:
        if self._turns.get(sample.turn_id, True):
            self._count("latency_samples_ignored")
            return False
        self._turns[sample.turn_id] = True
        self._count("latency_samples_recorded")
        payload = latency_sample_payload(sample.browser_playout_ms, sample.network_one_way_ms)
        evidence, turn_id, at = self._evidence, sample.turn_id, self._clock.utc_now()
        self._writer.submit(
            lambda: evidence.event(
                EventType.TRANSPORT_QUALITY_UPDATED,
                turn_id=turn_id,
                payload=payload,
                occurred_at=at,
            )
        )
        return True
