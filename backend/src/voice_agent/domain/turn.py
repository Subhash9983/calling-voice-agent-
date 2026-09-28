"""Conversation turn state machine (docs/01 §7, docs/02 §7, Decision 065/067 S8).

Transitions are branching. Guards encode the precedence rules:

- ``open -> transcript_final`` only with an accepted, non-blank transcript;
- ``open|transcript_final -> audio_streaming`` only for the deterministic
  clarification/fallback phrase;
- ``response_streaming -> completed`` only when no segment was speakable;
- ``open -> discarded`` only for empty/unusable/timed-out input.

Generated, synthesized, and spoken text are kept separate.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import Field

from voice_agent.contracts.base import CanonicalId, StrictModel
from voice_agent.contracts.enums import (
    FinishReason,
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    ResponseCompletionStatus,
    ResponseLanguage,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError

_S = TurnStatus
TURN_TRANSITIONS: Mapping[TurnStatus, frozenset[TurnStatus]] = MappingProxyType(
    {
        _S.OPEN: frozenset(
            {
                _S.TRANSCRIPT_FINAL,
                _S.AUDIO_STREAMING,
                _S.INTERRUPTED,
                _S.FAILED,
                _S.ABANDONED,
                _S.DISCARDED,
            }
        ),
        _S.TRANSCRIPT_FINAL: frozenset(
            {_S.RESPONSE_STREAMING, _S.AUDIO_STREAMING, _S.INTERRUPTED, _S.FAILED, _S.ABANDONED}
        ),
        _S.RESPONSE_STREAMING: frozenset(
            {_S.AUDIO_STREAMING, _S.COMPLETED, _S.INTERRUPTED, _S.FAILED, _S.ABANDONED}
        ),
        _S.AUDIO_STREAMING: frozenset({_S.COMPLETED, _S.INTERRUPTED, _S.FAILED, _S.ABANDONED}),
        _S.COMPLETED: frozenset(),
        _S.INTERRUPTED: frozenset(),
        _S.FAILED: frozenset(),
        _S.ABANDONED: frozenset(),
        _S.DISCARDED: frozenset(),
    }
)
TERMINAL_TURN_STATES: frozenset[TurnStatus] = frozenset(
    {_S.COMPLETED, _S.INTERRUPTED, _S.FAILED, _S.ABANDONED, _S.DISCARDED}
)
_UNUSABLE_INPUT: frozenset[InputDisposition] = frozenset(
    {InputDisposition.EMPTY, InputDisposition.UNUSABLE, InputDisposition.TIMED_OUT}
)
_COMPLETION_STATUSES: frozenset[ResponseCompletionStatus] = frozenset(
    {
        ResponseCompletionStatus.COMPLETED,
        ResponseCompletionStatus.TRUNCATED_PARTIAL,
        ResponseCompletionStatus.TRUNCATED_FALLBACK,
    }
)


class InterruptionSummary(StrictModel):
    detected_count: Annotated[int, Field(ge=0)] = 0
    accepted: bool = False
    false_interruption_suppressed_count: Annotated[int, Field(ge=0)] = 0
    phase: InterruptionPhase | None = None
    reason: InterruptionReason | None = None


class ConversationTurn(StrictModel):
    turn_id: CanonicalId
    session_id: CanonicalId
    sequence_number: Annotated[int, Field(ge=1)]
    status: TurnStatus = TurnStatus.OPEN
    status_revision: Annotated[int, Field(ge=0)] = 0
    input_disposition: InputDisposition = InputDisposition.PENDING
    response_completion_status: ResponseCompletionStatus = ResponseCompletionStatus.NOT_STARTED
    response_finish_reason: FinishReason | None = None
    final_transcript: str | None = None
    language: ResponseLanguage | None = None
    generated_text: str = ""
    synthesized_text: str = ""
    spoken_text: str = ""
    spoken_text_accuracy: SpokenTextAccuracy = SpokenTextAccuracy.UNAVAILABLE
    fallback_used: bool = False
    interruption: InterruptionSummary = InterruptionSummary()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TURN_STATES

    def _to(self, target: TurnStatus, **update: object) -> ConversationTurn:
        if target not in TURN_TRANSITIONS[self.status]:
            raise InvalidTransitionError("turn", self.status.value, target.value)
        return self.model_copy(
            update={"status": target, "status_revision": self.status_revision + 1, **update}
        )

    def _update(self, **update: object) -> ConversationTurn:
        if self.is_terminal:
            raise DomainRuleError("a terminal turn cannot be modified")
        return self.model_copy(update=update)

    def accept_transcript(self, text: str, language: ResponseLanguage | None) -> ConversationTurn:
        if not text.strip():
            raise DomainRuleError("an empty transcript cannot enter transcript_final")
        return self._to(
            TurnStatus.TRANSCRIPT_FINAL,
            input_disposition=InputDisposition.ACCEPTED,
            final_transcript=text,
            language=language,
        )

    def reject_input(self, disposition: InputDisposition) -> ConversationTurn:
        if disposition not in _UNUSABLE_INPUT:
            raise DomainRuleError("reject_input needs empty, unusable, or timed_out")
        if self.status is not TurnStatus.OPEN:
            raise DomainRuleError("input can be rejected only while the turn is open")
        return self._update(input_disposition=disposition)

    def discard(self) -> ConversationTurn:
        if self.input_disposition not in _UNUSABLE_INPUT:
            raise DomainRuleError("only empty or noise input can be discarded")
        return self._to(TurnStatus.DISCARDED)

    def start_response(self) -> ConversationTurn:
        return self._to(TurnStatus.RESPONSE_STREAMING)

    def authorize_audio(self, *, fallback: bool = False) -> ConversationTurn:
        """First authorized audio sets ``audio_streaming`` (idempotent afterwards)."""
        if self.status is TurnStatus.AUDIO_STREAMING:
            return self
        if self.status in {TurnStatus.OPEN, TurnStatus.TRANSCRIPT_FINAL} and not fallback:
            raise DomainRuleError("only the deterministic fallback may skip response streaming")
        if self.status is TurnStatus.OPEN and self.input_disposition not in _UNUSABLE_INPUT:
            raise DomainRuleError("open-turn fallback audio requires unusable input")
        return self._to(TurnStatus.AUDIO_STREAMING, fallback_used=self.fallback_used or fallback)

    def record_generated(self, text: str) -> ConversationTurn:
        return self._update(generated_text=self.generated_text + text)

    def record_synthesized(self, text: str) -> ConversationTurn:
        joined = f"{self.synthesized_text} {text}".strip()
        return self._update(synthesized_text=joined)

    def record_spoken(self, text: str, accuracy: SpokenTextAccuracy) -> ConversationTurn:
        joined = f"{self.spoken_text} {text}".strip()
        return self._update(spoken_text=joined, spoken_text_accuracy=accuracy)

    def record_finish_reason(self, reason: FinishReason) -> ConversationTurn:
        return self._update(response_finish_reason=reason)

    def record_interruption_candidate(self) -> ConversationTurn:
        summary = self.interruption.model_copy(
            update={"detected_count": self.interruption.detected_count + 1}
        )
        return self._update(interruption=summary)

    def record_false_interruption(self) -> ConversationTurn:
        count = self.interruption.false_interruption_suppressed_count + 1
        summary = self.interruption.model_copy(
            update={"false_interruption_suppressed_count": count}
        )
        return self._update(interruption=summary)

    def complete(
        self,
        completion: ResponseCompletionStatus = ResponseCompletionStatus.COMPLETED,
        *,
        no_speakable_output: bool = False,
    ) -> ConversationTurn:
        if completion not in _COMPLETION_STATUSES:
            raise DomainRuleError("completed turns record completed or truncated status only")
        if self.status is TurnStatus.RESPONSE_STREAMING and not no_speakable_output:
            raise DomainRuleError("completion without audio requires no speakable output")
        return self._to(TurnStatus.COMPLETED, response_completion_status=completion)

    def interrupt(
        self, *, reason: InterruptionReason, phase: InterruptionPhase | None
    ) -> ConversationTurn:
        summary = self.interruption.model_copy(
            update={"accepted": True, "reason": reason, "phase": phase}
        )
        return self._to(
            TurnStatus.INTERRUPTED,
            interruption=summary,
            response_completion_status=ResponseCompletionStatus.INTERRUPTED,
        )

    def fail(self) -> ConversationTurn:
        return self._to(
            TurnStatus.FAILED, response_completion_status=ResponseCompletionStatus.FAILED
        )

    def abandon(self) -> ConversationTurn:
        return self._to(TurnStatus.ABANDONED)
