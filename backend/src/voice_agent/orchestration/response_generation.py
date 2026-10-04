"""One authorized response generation for one accepted turn (docs/05 §12-§16, docs/08 §10-§14).

Provider-neutral: depends on the conversation port, the generation fence,
the ResponseSegmenter, and a delivery callback only.

- every adapter event is checked against the generation fence; the first
  stale verdict stops forwarding for good, requests provider cancellation,
  and from then on events only contribute usage evidence (late deltas are
  counted, never delivered);
- deltas feed the segmenter; each complete segment is re-checked against the
  fence and the disclosure guard immediately before delivery;
- a disclosure hit stops delivery of the whole response and cancels it;
- normal completion releases the final tail only if it is a complete valid
  unit; ``maximum_tokens`` discards an incomplete tail (docs/10 §8);
- generated text, delivered segments, and rejected/discarded evidence are
  kept separate in :class:`GenerationResult`.

:func:`resolve_completion` is the pure decision for the turn: the exact
``fallback.response_truncated.v1`` once when nothing meaningful was
delivered before the output cap, never a fabricated closure after a partial
delivery, and the response-failed template only when nothing was delivered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationFailed,
    ConversationRequest,
    ConversationTextDelta,
)
from voice_agent.contracts.enums import (
    FinishReason,
    ResponseCompletionStatus,
    ResponseLanguage,
    TtsLanguageCode,
)
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.usage import UsageReport
from voice_agent.orchestration.generations import FenceVerdict, GenerationFence
from voice_agent.orchestration.retry import RetryContext, RetryDecision, decide_retry
from voice_agent.ports.clock import Clock
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.response_segmentation.disclosure import DisclosureGuard, DisclosureReason
from voice_agent.response_segmentation.segmenter import (
    DiscardedTail,
    RejectedSegment,
    SegmenterState,
    SegmentOutcome,
    SpeakableSegment,
    feed,
    finish,
)
from voice_agent.turn_management.fallbacks import (
    RESPONSE_FAILED,
    RESPONSE_TRUNCATED,
    FallbackTemplate,
)


@dataclass(frozen=True, slots=True)
class DeliveredSegment:
    sequence: int
    text: str
    language_code: TtsLanguageCode
    fallback_template_id: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.fallback_template_id is not None


Deliver = Callable[[DeliveredSegment], Awaitable[None]]


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class GenerationResult:
    stamp: GenerationStamp
    finish_reason: FinishReason
    generated_text: str
    delivered: tuple[DeliveredSegment, ...]
    rejected: tuple[str, ...]
    tail_discarded: bool
    usage: UsageReport
    failure: NormalizedFailure | None
    cancelled: bool
    disclosure: DisclosureReason | None
    provider_request_id: str | None
    late_events: int
    first_token_ms: int | None
    first_segment_ms: int | None
    total_ms: int

    @property
    def delivered_text(self) -> str:
        return " ".join(segment.text for segment in self.delivered if not segment.is_fallback)

    @property
    def has_meaningful_delivery(self) -> bool:
        return any(not segment.is_fallback for segment in self.delivered)


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    status: ResponseCompletionStatus
    outcome: TurnOutcome
    fallback: FallbackTemplate | None


@dataclass
class _Run:
    request: ConversationRequest
    turn_stamp: GenerationStamp
    started_ms: int
    state: SegmenterState
    forwarding: bool = True
    superseded: bool = False
    disclosure: DisclosureReason | None = None
    delivered: list[DeliveredSegment] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    tail_discarded: bool = False
    late_events: int = 0
    first_token_ms: int | None = None
    first_segment_ms: int | None = None


class ResponseGenerator:
    """Runs one fenced generation attempt; owns no state between attempts."""

    def __init__(
        self,
        engine: ConversationEnginePort,
        *,
        fence: GenerationFence,
        guard: DisclosureGuard,
        deliver: Deliver,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._fence = fence
        self._guard = guard
        self._deliver = deliver
        self._clock = clock

    async def run(
        self, request: ConversationRequest, language: ResponseLanguage | None
    ) -> GenerationResult:
        run = _Run(
            request=request,
            turn_stamp=request.stamp.model_copy(update={"operation_id": None}),
            started_ms=self._clock.monotonic_ms(),
            state=SegmenterState(turn_language=language),
        )
        terminal: ConversationCompleted | ConversationCancelled | ConversationFailed | None = None
        events = self._engine.stream(request)
        try:
            async for event in events:
                await self._check_fence(run, event.stamp)
                if isinstance(event, ConversationTextDelta):
                    await self._on_delta(run, event.text)
                elif isinstance(
                    event, ConversationCompleted | ConversationCancelled | ConversationFailed
                ):
                    terminal = event
                    break
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
        if isinstance(terminal, ConversationCompleted) and run.forwarding:
            run.state, outcomes = finish(run.state, terminal.finish_reason)
            await self._deliver_outcomes(run, outcomes)
        return self._result(run, terminal)

    async def _check_fence(self, run: _Run, stamp: GenerationStamp) -> None:
        if not run.forwarding:
            return
        if self._fence.check(stamp) is not FenceVerdict.ACCEPTED:
            run.forwarding = False
            run.superseded = True
            await self._engine.cancel(run.request.stamp.operation_id or "")

    async def _on_delta(self, run: _Run, text: str) -> None:
        if not run.forwarding:
            run.late_events += 1
            return
        if run.first_token_ms is None:
            run.first_token_ms = self._clock.monotonic_ms() - run.started_ms
        run.state, outcomes = feed(run.state, text)
        await self._deliver_outcomes(run, outcomes)

    async def _deliver_outcomes(self, run: _Run, outcomes: list[SegmentOutcome]) -> None:
        for outcome in outcomes:
            if not run.forwarding:
                return
            if isinstance(outcome, DiscardedTail):
                run.tail_discarded = True
            elif isinstance(outcome, RejectedSegment):
                run.rejected.append(outcome.reason.value)
            elif isinstance(outcome, SpeakableSegment):
                await self._deliver_one(run, outcome)

    async def _deliver_one(self, run: _Run, segment: SpeakableSegment) -> None:
        if self._fence.check(run.turn_stamp) is not FenceVerdict.ACCEPTED:
            run.forwarding = False
            run.superseded = True
            await self._engine.cancel(run.request.stamp.operation_id or "")
            return
        reason = self._guard.check(segment.text)
        if reason is not None:
            run.disclosure = reason
            run.forwarding = False
            await self._engine.cancel(run.request.stamp.operation_id or "")
            return
        if run.first_segment_ms is None:
            run.first_segment_ms = self._clock.monotonic_ms() - run.started_ms
        delivered = DeliveredSegment(segment.sequence, segment.text, segment.language_code)
        run.delivered.append(delivered)
        await self._deliver(delivered)

    def _result(
        self,
        run: _Run,
        terminal: ConversationCompleted | ConversationCancelled | ConversationFailed | None,
    ) -> GenerationResult:
        finish_reason, usage, failure, request_id = _terminal_fields(terminal)
        cancelled = run.superseded or isinstance(terminal, ConversationCancelled)
        return GenerationResult(
            stamp=run.request.stamp,
            finish_reason=FinishReason.CANCELLED if cancelled else finish_reason,
            generated_text=run.state.generated_text,
            delivered=tuple(run.delivered),
            rejected=tuple(run.rejected),
            tail_discarded=run.tail_discarded,
            usage=usage,
            failure=failure,
            cancelled=cancelled,
            disclosure=run.disclosure,
            provider_request_id=request_id,
            late_events=run.late_events,
            first_token_ms=run.first_token_ms,
            first_segment_ms=run.first_segment_ms,
            total_ms=self._clock.monotonic_ms() - run.started_ms,
        )


def _terminal_fields(
    terminal: ConversationCompleted | ConversationCancelled | ConversationFailed | None,
) -> tuple[FinishReason, UsageReport, NormalizedFailure | None, str | None]:
    if isinstance(terminal, ConversationCompleted):
        return terminal.finish_reason, terminal.usage, None, terminal.provider_request_id
    if isinstance(terminal, ConversationFailed):
        return FinishReason.ERROR, terminal.usage, terminal.failure, None
    if isinstance(terminal, ConversationCancelled):
        return FinishReason.CANCELLED, terminal.usage, None, None
    return FinishReason.ERROR, UsageReport.unavailable(), None, None


_FAILED_FINISHES = frozenset({FinishReason.CONTENT_FILTERED, FinishReason.TOOL_CALL})


def resolve_completion(result: GenerationResult) -> CompletionDecision:
    """Turn completion and the single deterministic fallback, if any (docs/10 §5, §8)."""
    if result.cancelled and result.disclosure is None:
        return CompletionDecision(
            ResponseCompletionStatus.INTERRUPTED, TurnOutcome.INTERRUPTED, None
        )
    meaningful = result.has_meaningful_delivery
    failed = (
        result.disclosure is not None
        or result.failure is not None
        or result.finish_reason is FinishReason.ERROR
        or result.finish_reason in _FAILED_FINISHES
    )
    if not failed and result.finish_reason is FinishReason.MAXIMUM_TOKENS:
        if meaningful:
            return CompletionDecision(
                ResponseCompletionStatus.TRUNCATED_PARTIAL, TurnOutcome.COMPLETED, None
            )
        return CompletionDecision(
            ResponseCompletionStatus.TRUNCATED_FALLBACK, TurnOutcome.COMPLETED, RESPONSE_TRUNCATED
        )
    if not failed and meaningful:
        return CompletionDecision(ResponseCompletionStatus.COMPLETED, TurnOutcome.COMPLETED, None)
    fallback = None if meaningful else RESPONSE_FAILED
    return CompletionDecision(ResponseCompletionStatus.FAILED, TurnOutcome.FAILED, fallback)


def retry_decision(
    policy: RetryPolicy, result: GenerationResult, *, attempt_number: int, jitter: float
) -> RetryDecision | None:
    """``None`` when the attempt did not fail; never retries after any delivery."""
    if result.failure is None or result.cancelled or result.disclosure is not None:
        return None
    context = RetryContext(attempt_number=attempt_number, output_delivered=bool(result.delivered))
    return decide_retry(policy, result.failure, context, jitter=jitter)


async def deliver_fallback(
    template: FallbackTemplate,
    *,
    fence: GenerationFence,
    turn_stamp: GenerationStamp,
    language: ResponseLanguage | None,
    deliver: Deliver,
) -> tuple[DeliveredSegment, ...]:
    """Deliver the approved deterministic phrase once, only while still authorized."""
    state, outcomes = feed(SegmenterState(turn_language=language), template.text)
    _, tail = finish(state, FinishReason.COMPLETED)
    delivered: list[DeliveredSegment] = []
    for outcome in [*outcomes, *tail]:
        if not isinstance(outcome, SpeakableSegment):
            continue
        if fence.check(turn_stamp) is not FenceVerdict.ACCEPTED:
            break
        segment = DeliveredSegment(
            outcome.sequence, outcome.text, outcome.language_code, template.template_id
        )
        delivered.append(segment)
        await deliver(segment)
    return tuple(delivered)
