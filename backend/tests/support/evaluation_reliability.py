"""Offline ``ReliabilityExecutor`` for REL-091..100 over the WP10 conversation rig.

Each docs/17 §19 fault scenario runs the real WP10 orchestrator, Turn
Manager, mock VAD, Deepgram/OpenAI adapters over scripted fakes, and the
mock TTS (no network, no key, INR 0). Timings are scaled (e.g. the 3,000 ms
STT finalization timeout runs at 100 ms, the 30-minute limit uses a deadline
inside the notice window); the case parameters keep the approved values.

The observation is built from the same durable evidence a real session
writes: turns, operations, events, cost entries, and errors, priced and
reconciled through the WP11 :func:`build_session_evidence`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from tests.support.conversation_rig import FAR, SILENCE, SPEECH, Rig, build
from tests.support.fake_deepgram import FakeDeepgramConnector
from tests.support.fake_openai import PAUSE, created, delta, failed, reply
from tests.support.fake_session_transport import SESSION_ID
from tests.support.persistence_builders import make_config, make_session

from voice_agent.agent_worker.conversation_policy import ConversationTimeouts
from voice_agent.contracts.enums import TurnStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.transport import ClientReady, TransportEvent, TransportEventKind
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.evaluation.case import EvaluationCase, ReliabilityInput
from voice_agent.domain.turn import ConversationTurn
from voice_agent.evaluation.cost_evidence import evidence_from_reconciliation
from voice_agent.evaluation.live_evidence import lifecycle_ordered, media_persisted
from voice_agent.evaluation.observations import HarnessError, ReliabilityObservation
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.tts_adapters.mock.adapter import MockTtsAdapter
from voice_agent.turn_management.greeting import OPENING_GREETING

_TERMINAL_TURN_EVENTS = (
    EventType.TURN_COMPLETED,
    EventType.TURN_INTERRUPTED,
    EventType.TURN_FAILED,
)


@dataclass(frozen=True)
class _Marks:
    """State captured just before the fault trigger."""

    old_generation: int | None = None
    old_frames: int = 0
    llm_requests: int = 0


def _event(kind: TransportEventKind) -> TransportEvent:
    return TransportEvent(kind=kind, at_ms=0)


def _frames(rig: Rig, generation: int | None) -> int:
    if generation is None:
        return 0
    return sum(
        1 for f in rig.transport.published if f.identity.cancellation_generation == generation
    )


def _marks(rig: Rig) -> _Marks:
    published = rig.transport.published
    generation = published[-1].identity.cancellation_generation if published else None
    return _Marks(generation, _frames(rig, generation), len(rig.llm.params))


def _stale_history(rig: Rig, tts: MockTtsAdapter) -> bool:
    """History holds assistant text whose TTS segment never produced a published frame."""
    played = {f.identity.segment_id for f in rig.transport.published}
    unheard = [r.text for r in tts.requests if r.segment_id not in played]
    history = " ".join(m.text for m in rig.gate.history if m.role.value == "assistant")
    return any(text.strip() and text.strip() in history for text in unheard)


async def _cost(rig: Rig) -> Any:
    operations = list(await rig.operations.list_for_session(SESSION_ID))
    entries = [entry for run in rig.costs.runs for entry in run]
    session = make_session(make_config()).model_copy(update={"session_id": SESSION_ID})
    evidence = build_session_evidence(
        SessionEvidenceInput(
            session=session,
            turns=await rig.all_turns(),
            events=rig.events.records,
            operations=operations,
            cost_entries=entries,
            errors=rig.errors.records,
        ),
        card=phase0_rate_card(),
    )
    card = phase0_rate_card()
    return evidence_from_reconciliation(
        evidence.cost,
        operations=operations,
        entries=entries,
        rate_card_id=card.rate_card_id,
        card=card,
    )


def _max_submissions(tts: MockTtsAdapter) -> int:
    """How many times the same segment text reached TTS (1 = no retry/loop)."""
    counts: dict[str, int] = {}
    for request in tts.requests:
        counts[request.text] = counts.get(request.text, 0) + 1
    return max(counts.values(), default=0)


def _fallbacks(rig: Rig) -> tuple[str, ...]:
    ids = [f["payload"].get("fallback_template_id") for f in rig.finals()]
    return tuple(i for i in ids if i)


def _llm_attempts(operations: list[Any]) -> int:
    return sum(1 for op in operations if op.component.value == "conversation_engine")


def _terminal_events(rig: Rig, turn_id: str) -> int:
    return sum(
        1
        for r in rig.events.records
        if r.envelope.turn_id == turn_id and r.envelope.event_type in _TERMINAL_TURN_EVENTS
    )


async def _base(
    rig: Rig, tts: MockTtsAdapter, scenario: str, marks: _Marks, **fields: Any
) -> ReliabilityObservation:
    operations = list(await rig.operations.list_for_session(SESSION_ID))
    failures = tuple(sorted({e.error_type.value for e in rig.errors.records}))
    defaults: dict[str, Any] = {
        "scenario": scenario,
        "cost": await _cost(rig),
        "llm_requests_after_trigger": len(rig.llm.params) - marks.llm_requests,
        "llm_requests_total": len(rig.llm.params),
        "greeting_count": _fallbacks(rig).count(OPENING_GREETING.template_id),
        "stale_audio_frames": max(_frames(rig, marks.old_generation) - marks.old_frames, 0),
        "stale_history": _stale_history(rig, tts),
        "accepted_interruptions": rig.orchestrator.counters.get("interruptions_accepted", 0),
        "suppressed_interruptions": rig.orchestrator.counters.get(
            "false_interruptions_suppressed", 0
        ),
        "fallback_template_ids": _fallbacks(rig),
        "failure_types": failures,
        "llm_attempts": _llm_attempts(operations),
        "tts_attempts": _max_submissions(tts),
        "lifecycle_ordered": lifecycle_ordered(rig.events.records),
        "persisted_audio_or_partial": media_persisted(rig.events.records),
        "session_end_reason": rig.ended[0].value if rig.ended else None,
    }
    return ReliabilityObservation(**{**defaults, **fields})


# ------------------------------------------------------------ scenarios --
async def _barge_in(case: EvaluationCase, *, mid_response: bool) -> ReliabilityObservation:
    params = case.input.fault_parameters if isinstance(case.input, ReliabilityInput) else None
    utterance = str((params or {}).get("barge_in_utterance", "Ruko"))
    if mid_response:
        old = reply("Pehli line. ", "Doosri line. ", "Teesri line.")
        tts = MockTtsAdapter(frames_per_segment=3, pause_when=lambda r: "Doosri" in r.text)
        new = reply("Ek line mein summary yeh hai ki pehle zaroori kaam karo.")
    else:
        old = reply("Yeh ek lamba jawab hai.")
        tts = MockTtsAdapter(frames_per_segment=6, pause_when=lambda r: "lamba" in r.text)
        new = reply("Ji, boliye.")
    rig = build([old, new], ["Sab batao", utterance], tts=tts)
    await rig.start()
    await rig.utterance()
    await rig.until(lambda: len(rig.transport.published) >= (4 if mid_response else 1))
    marks = _marks(rig)
    await rig.feed(15, SPEECH)  # 300 ms of continuous speech over agent audio
    await rig.feed(40, SILENCE)
    await rig.idle()
    await rig.stop()
    first, *rest = await rig.all_turns()
    responses = [t for t in rest if t.status is TurnStatus.COMPLETED and not t.fallback_used]
    return await _base(
        rig,
        tts,
        case.case_key,
        marks,
        old_generation_cancelled=first.status is TurnStatus.INTERRUPTED,
        new_responses=len(responses),
        new_response_text=responses[0].spoken_text if responses else None,
        interruption_latency_ms=first.interruption.interruption_latency_ms,
        turn_ids=tuple(t.turn_id for t in (first, *rest)),
    )


async def _false_interruption(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=3)
    rig = build([reply("Main bol rahi hoon.")], ["Bolo"], tts=tts)
    rig.transport.auto_playout = False
    await rig.start()
    await rig.utterance()
    await rig.until(lambda: rig.transport.playouts >= 1)
    marks = _marks(rig)
    playouts_before = rig.transport.playouts
    await rig.feed(10, SPEECH)  # 200 ms cough/tap: below the 250 ms confirmation
    await rig.feed(30, SILENCE)
    rig.transport.release_playout()
    await rig.idle()
    await rig.stop()
    return await _base(
        rig,
        tts,
        case.case_key,
        marks,
        playouts_after_trigger=rig.transport.playouts - playouts_before + 1,
        cancellation_increments=rig.transport.clears,
    )


async def _greeted(rig: Rig) -> None:
    rig.transport.client(ClientReady())
    await rig.until(lambda: bool(rig.finals()))
    await rig.idle()


async def _disconnect_during_playback(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=4, pause_when=lambda r: "lamba" in r.text)
    rig = build([reply("Yeh lamba jawab hai.")], ["Batao"], tts=tts, greeting=True)
    await rig.start()
    await _greeted(rig)
    await rig.utterance()
    await rig.until(lambda: any("lamba" in r.text for r in tts.requests))
    await rig.until(
        lambda: bool(_frames(rig, rig.transport.published[-1].identity.cancellation_generation))
    )
    marks = _marks(rig)
    left = time.monotonic()
    rig.orchestrator.on_transport_event(_event(TransportEventKind.BROWSER_LEFT))
    await rig.until(lambda: rig.orchestrator._paused)
    await rig.idle()
    await asyncio.sleep(0.05)  # stands in for the 5 s outage inside the 20 s window
    rig.orchestrator.on_transport_event(_event(TransportEventKind.RECONNECTED))
    reconnect_ms = int((time.monotonic() - left) * 1000)
    rig.transport.client(ClientReady())
    await asyncio.sleep(0.05)
    await rig.stop()
    turns = await rig.all_turns()
    spoken = [t for t in turns if t.final_transcript == "Batao"]
    return await _base(
        rig,
        tts,
        case.case_key,
        marks,
        old_generation_cancelled=bool(spoken) and spoken[0].status is TurnStatus.INTERRUPTED,
        reconnect_ms=reconnect_ms,
        duplicate_turns=max(len(spoken) - 1, 0),
    )


async def _disconnect_before_llm(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2)
    late = [
        created(),
        PAUSE,
        delta("Yeh late jawab hai."),
        {
            "type": "response.completed",
            "sequence_number": 9,
            "response": {"id": "resp_fake_0001", "status": "completed"},
        },
    ]
    rig = build([late, reply("Wapas aa gaye.")], ["Batao", "Main wapas"], tts=tts)
    await rig.start()
    await rig.utterance()
    await rig.until(lambda: len(rig.llm.params) == 1)
    marks = _marks(rig)
    rig.orchestrator.on_transport_event(_event(TransportEventKind.BROWSER_LEFT))
    await rig.until(lambda: rig.orchestrator._paused)
    await rig.idle()
    rig.orchestrator.on_transport_event(_event(TransportEventKind.RECONNECTED))
    await rig.until(lambda: not rig.orchestrator._paused)
    await rig.utterance()
    await rig.idle()
    await rig.stop()
    old, *_rest = await rig.all_turns()
    late_spoken = any("late" in r.text for r in tts.requests)
    return await _base(
        rig,
        tts,
        case.case_key,
        marks,
        old_generation_cancelled=old.status is not TurnStatus.COMPLETED,
        stale_audio_frames=1 if late_spoken else 0,
        terminal_dispositions=_terminal_events(rig, old.turn_id),
    )


async def _stt_timeout(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2)
    rig = build(
        tts=tts, deepgram=FakeDeepgramConnector(answer_finalize=False), finalize_timeout_ms=100
    )
    await rig.start()
    marks = _marks(rig)
    await rig.utterance()
    await rig.until(lambda: bool(rig.finals()))
    await rig.idle()
    await rig.stop()
    turns: list[ConversationTurn] = await rig.all_turns()
    timed_out = tuple(
        t.input_disposition.value for t in turns if t.input_disposition.value == "timed_out"
    )
    observation = await _base(rig, tts, case.case_key, marks)
    return _with(observation, failure_types=(*observation.failure_types, *timed_out))


async def _llm_failure(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2)
    broken = [failed("server_error")]
    rig = build([broken, broken, broken], ["Pehle"], tts=tts)
    await rig.start()
    marks = _marks(rig)
    await rig.utterance()
    await rig.idle()
    await rig.stop()
    operations = list(await rig.operations.list_for_session(SESSION_ID))
    failures = tuple(
        sorted({op.failure.error_type.value for op in operations if op.failure is not None})
    )
    observation = await _base(rig, tts, case.case_key, marks)
    return _with(observation, failure_types=(*observation.failure_types, *failures))


async def _tts_failure(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2, fail_when=lambda _r: True)
    rig = build([reply("Yeh jawab kabhi bola nahi jayega.")], ["Batao"], tts=tts)
    await rig.start()
    marks = _marks(rig)
    await rig.utterance()
    await rig.idle()
    await rig.stop()
    [turn] = await rig.all_turns()
    operations = list(await rig.operations.list_for_session(SESSION_ID))
    failures = tuple(
        sorted({op.failure.error_type.value for op in operations if op.failure is not None})
    )
    observation = await _base(
        rig,
        tts,
        case.case_key,
        marks,
        generated_text_delivered=bool((turn.spoken_text or "").strip()),
    )
    return _with(
        observation,
        failure_types=(*observation.failure_types, *failures),
        stale_audio_frames=len(rig.transport.published),
    )


async def _greeting_once(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2)
    rig = build(tts=tts, greeting=True)
    await rig.start()
    marks = _marks(rig)
    rig.transport.client(ClientReady())
    rig.transport.client(ClientReady())
    await rig.until(lambda: bool(rig.finals()))
    await rig.idle()
    rig.orchestrator.on_transport_event(_event(TransportEventKind.BROWSER_LEFT))
    rig.orchestrator.on_transport_event(_event(TransportEventKind.RECONNECTED))
    rig.transport.client(ClientReady())
    await asyncio.sleep(0.05)
    await rig.stop()
    return await _base(rig, tts, case.case_key, marks, greeting_llm_calls=len(rig.llm.params))


async def _time_limit(case: EvaluationCase) -> ReliabilityObservation:
    tts = MockTtsAdapter(frames_per_segment=2)
    deadline = SystemClock().utc_now() + timedelta(seconds=5)  # inside the notice window
    rig = build(
        tts=tts,
        deadline_at=deadline,
        timeouts=ConversationTimeouts(
            maximum_silence_ms=FAR,
            maximum_user_turn_ms=FAR,
            idle_session_ms=FAR,
            maximum_duration_deadline_at=deadline,
            tick_s=0.01,
        ),
    )
    await rig.start()
    marks = _marks(rig)
    await rig.until(lambda: bool(rig.ended))
    await rig.utterance()  # nothing is accepted after the limit
    await rig.stop()
    turns = await rig.all_turns()
    return await _base(rig, tts, case.case_key, marks, turns_after_limit=max(len(turns) - 1, 0))


def _with(observation: ReliabilityObservation, **changes: Any) -> ReliabilityObservation:
    return replace(observation, **changes)


Scenario = Callable[[EvaluationCase], Awaitable[ReliabilityObservation]]
SCENARIOS: dict[str, Scenario] = {
    "barge_in_first_segment": lambda c: _barge_in(c, mid_response=False),
    "barge_in_mid_response": lambda c: _barge_in(c, mid_response=True),
    "false_interruption_short_noise": _false_interruption,
    "disconnect_during_playback": _disconnect_during_playback,
    "disconnect_before_llm_complete": _disconnect_before_llm,
    "stt_finalization_timeout": _stt_timeout,
    "llm_failure_before_segment": _llm_failure,
    "tts_failure_before_audio": _tts_failure,
    "duplicate_ready_and_reconnect": _greeting_once,
    "maximum_session_duration": _time_limit,
}


class RigReliabilityExecutor:
    """Runs the case's fault scenario; an unknown scenario is a test-setup error."""

    def __init__(self, overrides: dict[str, Scenario] | None = None) -> None:
        self._scenarios = {**SCENARIOS, **(overrides or {})}
        self.executed: list[tuple[str, int]] = []

    async def execute(self, case: EvaluationCase, repetition: int) -> ReliabilityObservation:
        if not isinstance(case.input, ReliabilityInput):
            raise HarnessError("test_setup_invalid", "not a reliability case")
        scenario = self._scenarios.get(case.input.fault_scenario_code)
        if scenario is None:
            raise HarnessError("test_setup_invalid", "unknown fault scenario")
        self.executed.append((case.case_key, repetition))
        async with asyncio.timeout(20):
            return await scenario(case)
