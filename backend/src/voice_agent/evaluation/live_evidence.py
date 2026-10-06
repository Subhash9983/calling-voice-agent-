"""Live voice and ``INT-LIVE`` evidence from stored WP11 session evidence.

The manual browser protocol (docs/17 §13, §19A) produces ordinary R&D
sessions; nothing new is recorded. A live case's observation is derived
from that session's durable turns, events, attempts, and cost lines through
the same :func:`build_session_evidence` / :func:`turn_latency` /
:func:`summarize` used by the operational report and the WP10 gate.
Ordinary audio is never involved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Final

from voice_agent.contracts.cost import RateCard
from voice_agent.contracts.enums import OperationComponent
from voice_agent.contracts.events import EventType
from voice_agent.contracts.usage import UsageUnit
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.turn import ConversationTurn
from voice_agent.evaluation.cost_evidence import evidence_from_reconciliation
from voice_agent.evaluation.observations import HarnessError, LiveObservation
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.events_and_latency.interruption import (
    InterruptionLatencySummary,
    samples_from_turns,
    summarize,
)
from voice_agent.events_and_latency.latency import (
    FIRST_AUDIBLE_RESPONSE,
    LLM_FIRST_TOKEN,
    STT_FINALIZATION,
    TTS_FIRST_AUDIO,
    turn_latency,
)
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.ports.control_plane import EventRecord

FINALIZED_EVENTS: Final = frozenset({EventType.STT_TURN_FINALIZED, EventType.STT_FINAL})
MEDIA_EVENTS: Final = frozenset({EventType.STT_PARTIAL, EventType.TTS_AUDIO_FRAME})
MEDIA_PAYLOAD_KEYS: Final = frozenset({"audio", "pcm", "partial_transcript"})
PLAYBACK_EVENTS: Final = frozenset({EventType.PLAYBACK_STARTED, EventType.PLAYBACK_COMPLETED})
SessionLoader = Callable[[str], Awaitable[SessionEvidenceInput | None]]


def lifecycle_ordered(events: Iterable[EventRecord]) -> bool:
    """S-LIFECYCLE: no generation starts before its turn's transcript was finalized."""
    finalized: set[str | None] = set()
    for record in events:
        envelope = record.envelope
        if envelope.event_type in FINALIZED_EVENTS:
            finalized.add(envelope.turn_id)
        if envelope.event_type is EventType.CONVERSATION_STARTED and (
            envelope.turn_id not in finalized
        ):
            return False
    return True


def media_persisted(events: Iterable[EventRecord]) -> bool:
    """S-NOAUDIO: an audio frame or partial transcript reached durable events."""
    return any(
        record.envelope.event_type in MEDIA_EVENTS
        or bool(MEDIA_PAYLOAD_KEYS & set(record.envelope.payload))
        for record in events
    )


def _user_turn(turns: Sequence[ConversationTurn]) -> ConversationTurn | None:
    """The scripted utterance: the first turn with an accepted final transcript."""
    ordered = sorted(turns, key=lambda turn: turn.sequence_number)
    return next((t for t in ordered if t.final_transcript and not t.fallback_used), None)


def _output_tokens(data: SessionEvidenceInput, turn_id: str) -> int | None:
    for operation in data.operations:
        if operation.turn_id != turn_id:
            continue
        if operation.component is not OperationComponent.CONVERSATION_ENGINE:
            continue
        for item in operation.usage.items:
            if item.unit is UsageUnit.OUTPUT_TOKENS:
                return int(item.quantity)
    return None


def live_observation(data: SessionEvidenceInput, *, card: RateCard | None) -> LiveObservation:
    evidence = build_session_evidence(data, card=card)
    cost = evidence_from_reconciliation(
        evidence.cost,
        operations=data.operations,
        entries=data.cost_entries,
        rate_card_id=data.session.cost_rate_card_version,
        card=card,
    )
    ordered, persisted = lifecycle_ordered(data.events), media_persisted(data.events)
    session_id = data.session.session_id
    turn = _user_turn(data.turns)
    if turn is None:
        return LiveObservation(
            accepted_final_transcript=None,
            response_text=None,
            cost=cost,
            playback_confirmed=False,
            lifecycle_ordered=ordered,
            persisted_audio_or_partial=persisted,
            session_id=session_id,
        )
    events = [r for r in data.events if r.envelope.turn_id == turn.turn_id]
    operations = [op for op in data.operations if op.turn_id == turn.turn_id]
    latency = turn_latency(turn, [r.envelope for r in events], operations)
    samples, audible = latency.samples, latency.first_audible
    return LiveObservation(
        accepted_final_transcript=turn.final_transcript,
        response_text=turn.spoken_text or None,
        cost=cost,
        playback_confirmed=any(r.envelope.event_type in PLAYBACK_EVENTS for r in events),
        output_tokens=_output_tokens(data, turn.turn_id),
        # Composed only (docs/11 §11); a worker-only sample never fills this field.
        speech_end_to_playback_ms=samples.get(FIRST_AUDIBLE_RESPONSE),
        speech_end_to_playback_method=None if audible is None else audible.method,
        speech_end_network_uncertainty_ms=None
        if audible is None
        else audible.network_uncertainty_ms,
        speech_end_to_worker_audio_ms=None if audible is None else audible.worker_span_ms,
        stt_final_ms=samples.get(STT_FINALIZATION),
        llm_first_token_ms=samples.get(LLM_FIRST_TOKEN),
        tts_first_audio_ms=samples.get(TTS_FIRST_AUDIO),
        lifecycle_ordered=ordered,
        persisted_audio_or_partial=persisted,
        session_id=session_id,
        turn_ids=(turn.turn_id,),
    )


class SessionEvidenceLiveExecutor:
    """Maps each live case to the R&D session the tester ran for it (manual protocol)."""

    def __init__(
        self,
        sessions: Mapping[str, str],
        loader: SessionLoader,
        *,
        card_for: Callable[[str], RateCard | None],
    ) -> None:
        self._sessions = dict(sessions)
        self._loader = loader
        self._card_for = card_for

    async def execute(self, case: EvaluationCase, repetition: int) -> LiveObservation:
        session_id = self._sessions.get(case.case_key)
        if session_id is None:
            raise HarnessError("test_setup_invalid", "no live session recorded for this case")
        data = await self._loader(session_id)
        if data is None:
            raise HarnessError("test_setup_invalid", "the live session evidence is missing")
        return live_observation(data, card=self._card_for(data.session.cost_rate_card_version))


async def interruption_evidence(
    session_ids: Sequence[str], loader: SessionLoader
) -> InterruptionLatencySummary:
    """``INT-LIVE`` (plus any live barge-in) samples through the WP10 gate evaluator."""
    turns: list[ConversationTurn] = []
    for session_id in session_ids:
        data = await loader(session_id)
        if data is not None:
            turns.extend(data.turns)
    return summarize(samples_from_turns(turns))
