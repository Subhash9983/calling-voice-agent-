"""LLM-check generation gate: accepted durable transcript -> one GPT-6 Luna response (WP8).

Plugs into :class:`SttCheck` as its :class:`GenerationGate`
(docs/05 §12-§16, docs/07 §15, docs/08, docs/10 §5-§8):

- only a turn in ``transcript_final`` with an accepted, non-blank transcript
  is authorized, and each turn ID authorizes **exactly one** logical
  generation; a duplicate trigger is counted and ignored;
- a newer accepted turn supersedes an active generation: the gate's
  generation fence advances first (late output can no longer be delivered),
  then the provider stream is cancelled; the old turn ends ``interrupted``
  with only its delivered portion in history;
- a transient failure before any delivery may retry with a new operation ID
  (same logical request and turn); delivered output is never restarted;
- there is no TTS in this check: each authorized segment is "delivered" by
  publishing the cumulative delivered text on ``va.response.v1``;
- generated text (the turn record), delivered text (history and browser), and
  rejected/discarded evidence (operation summary counts) stay separate;
- the application-owned fallback phrase is used only when nothing meaningful
  was delivered, once, and only while the turn is still authorized.

The fence here is the gate's own: STT stamps belong to the STT check's fence,
so superseding a response never invalidates an in-flight transcript.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from pydantic import JsonValue

from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.conversation import ConversationRequest, HistoryMessage, HistoryRole
from voice_agent.contracts.enums import (
    AgentActivityState,
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.generations import FenceVerdict, GenerationFence
from voice_agent.orchestration.history_budget import HistoryBudget, budget_history
from voice_agent.orchestration.response_generation import (
    CompletionDecision,
    DeliveredSegment,
    GenerationResult,
    ResponseGenerator,
    TurnOutcome,
    deliver_fallback,
    resolve_completion,
    retry_decision,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.response_segmentation.disclosure import DisclosureGuard

GENERATE_RESPONSE: Final = "generate_response"
CLOSE_TIMEOUT_S: Final = 5.0
MS_PER_SECOND: Final = 1000
_LOGGER = logging.getLogger("voice_agent.agent_worker.llm")


@dataclass(frozen=True, slots=True)
class ConversationSetup:
    session_id: str
    correlation_id: str
    worker_generation: int
    agent_config_id: str
    config_checksum: str
    prompt_id: str
    prompt_version: int
    prompt_checksum: str
    system_instruction: str
    max_output_tokens: int
    provider: str
    model: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class _TurnRun:
    turn: ConversationTurn
    delivered: list[DeliveredSegment] = field(default_factory=list)
    operation_id: str | None = None

    def delivered_text(self) -> str:
        return " ".join(segment.text for segment in self.delivered)


def _acceptable(turn: ConversationTurn) -> bool:
    return (
        turn.status is TurnStatus.TRANSCRIPT_FINAL
        and turn.input_disposition is InputDisposition.ACCEPTED
        and bool((turn.final_transcript or "").strip())
    )


class ConversationGate:
    def __init__(
        self,
        setup: ConversationSetup,
        *,
        engine: ConversationEnginePort,
        evidence: SttEvidence,
        publisher: RealtimePublisher,
        clock: Clock,
        ids: IdGenerator,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._setup = setup
        self._engine = engine
        self._evidence = evidence
        self._publisher = publisher
        self._clock = clock
        self._ids = ids
        self._jitter = jitter
        self._fence = GenerationFence(
            session_id=setup.session_id, worker_generation=setup.worker_generation
        )
        self._guard = DisclosureGuard.for_instruction(setup.system_instruction)
        self._authorized: set[str] = set()
        self._active: _TurnRun | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self.history: list[HistoryMessage] = []
        self.counters: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    # --------------------------------------------------------- authorize --
    async def authorize(self, turn: ConversationTurn) -> bool:
        if self._closing or not _acceptable(turn):
            self._count("rejected_triggers")
            return False
        if turn.turn_id in self._authorized:
            self._count("duplicate_triggers")
            return True
        self._authorized.add(turn.turn_id)
        await self._supersede_active()
        self._fence.activate_turn(turn.turn_id)
        started = turn.start_response()
        await self._evidence.save_turn(started)
        run = _TurnRun(turn=started)
        self._active = run
        await self._publisher.publish_state(AgentActivityState.THINKING)
        self._tasks[turn.turn_id] = asyncio.create_task(self._respond(run))
        return True

    async def _supersede_active(self) -> None:
        active = self._active
        if active is None:
            return
        self._count("superseded_generations")
        self._fence.advance()  # fence first: no later output of the old turn is delivered
        if active.operation_id is not None:
            await self._engine.cancel(active.operation_id)

    # ----------------------------------------------------------- respond --
    async def _respond(self, run: _TurnRun) -> None:
        try:
            result = await self._generate(run)
            await self._finish(run, result)
        except Exception as error:  # a crashed generation never takes the session down
            _LOGGER.warning(
                "llm.generation_crashed", extra={"safe_fields": {"error": type(error).__name__}}
            )
            await self._crashed(run)
        finally:
            if self._active is run:
                self._active = None
                self._fence.deactivate_turn(run.turn.turn_id)

    async def _generate(self, run: _TurnRun) -> GenerationResult:
        transcript = run.turn.final_transcript or ""
        budget = budget_history(
            self.history,
            system_instruction=self._setup.system_instruction,
            user_transcript=transcript,
        )
        logical_request_id = self._ids.new_id()
        operation = self._new_operation(run, logical_request_id)
        while True:
            result = await self._attempt(run, operation, budget)
            decision = retry_decision(
                self._setup.retry,
                result,
                attempt_number=operation.attempt_number,
                jitter=self._jitter(),
            )
            if decision is None or not decision.should_retry or not self._still_current(run):
                return result
            self._count("retries")
            await asyncio.sleep((decision.backoff_ms or 0) / MS_PER_SECOND)
            if not self._still_current(run):
                return result
            operation = operation.next_attempt(self._ids.new_id())

    def _new_operation(self, run: _TurnRun, logical_request_id: str) -> ProviderOperation:
        return ProviderOperation(
            operation_id=self._ids.new_id(),
            logical_request_id=logical_request_id,
            session_id=self._setup.session_id,
            turn_id=run.turn.turn_id,
            component=OperationComponent.CONVERSATION_ENGINE,
            operation_type=GENERATE_RESPONSE,
            provider=self._setup.provider,
            model=self._setup.model,
            worker_generation=self._setup.worker_generation,
        )

    def _still_current(self, run: _TurnRun) -> bool:
        stamp = self._fence.stamp(turn_id=run.turn.turn_id)
        return not self._closing and self._fence.check(stamp) is FenceVerdict.ACCEPTED

    def _request(
        self, run: _TurnRun, operation: ProviderOperation, budget: HistoryBudget
    ) -> ConversationRequest:
        setup = self._setup
        return ConversationRequest(
            stamp=self._fence.stamp(turn_id=run.turn.turn_id, operation_id=operation.operation_id),
            logical_request_id=operation.logical_request_id,
            correlation_id=setup.correlation_id,
            agent_config_id=setup.agent_config_id,
            config_checksum=setup.config_checksum,
            system_instruction_id=setup.prompt_id,
            system_instruction_version=setup.prompt_version,
            system_instruction=setup.system_instruction,
            user_transcript=run.turn.final_transcript or "",
            history=budget.messages,
            language=run.turn.language,
            max_output_tokens=setup.max_output_tokens,
        )

    async def _attempt(
        self, run: _TurnRun, operation: ProviderOperation, budget: HistoryBudget
    ) -> GenerationResult:
        request = self._request(run, operation, budget)
        started = operation.transition_to(OperationStatus.STARTED, started_at=self._clock.utc_now())
        self._fence.register_operation(started.operation_id)
        run.operation_id = started.operation_id
        await self._evidence.save_operation(started)
        await self._evidence.event(
            EventType.CONVERSATION_STARTED,
            turn_id=run.turn.turn_id,
            operation_id=started.operation_id,
            payload={"attempt_number": started.attempt_number, **_budget_evidence(budget)},
        )

        async def deliver(segment: DeliveredSegment) -> None:
            run.delivered.append(segment)
            await self._publisher.publish_response_segment(
                run.delivered_text(), turn_id=run.turn.turn_id, segment_sequence=segment.sequence
            )

        generator = ResponseGenerator(
            self._engine, fence=self._fence, guard=self._guard, deliver=deliver, clock=self._clock
        )
        try:
            result = await generator.run(request, run.turn.language)
        finally:
            self._fence.retire_operation(started.operation_id)
        await self._settle(started, result, budget)
        return result

    async def _settle(
        self, operation: ProviderOperation, result: GenerationResult, budget: HistoryBudget
    ) -> None:
        if result.cancelled:
            done = operation.cancel(result.usage)
            event_type = EventType.CONVERSATION_CANCELLED
        elif result.failure is not None:
            done = operation.fail(result.failure, result.usage)
            event_type = EventType.CONVERSATION_FAILED
        else:
            done = operation.succeed(result.usage)
            event_type = EventType.CONVERSATION_COMPLETED
        first_at = None
        if operation.started_at is not None and result.first_token_ms is not None:
            first_at = operation.started_at + timedelta(milliseconds=result.first_token_ms)
        done = done.model_copy(
            update={
                "first_result_at": first_at,
                "time_to_first_result_ms": result.first_token_ms,
                "total_duration_ms": result.total_ms,
                "result_summary": self._summary(result, budget),
            }
        )
        await self._evidence.operation_settled(done)
        await self._evidence.event(
            event_type,
            turn_id=operation.turn_id,
            operation_id=operation.operation_id,
            payload={
                "finish_reason": result.finish_reason.value,
                "delivered_segments": len(result.delivered),
                "late_events": result.late_events,
            },
            severity=EventSeverity.WARNING if result.failure else EventSeverity.INFO,
        )
        await self._evidence.event(
            EventType.CONVERSATION_USAGE,
            turn_id=operation.turn_id,
            operation_id=operation.operation_id,
            payload={"reporting_status": result.usage.reporting_status.value},
        )

    def _summary(self, result: GenerationResult, budget: HistoryBudget) -> dict[str, JsonValue]:
        setup = self._setup
        summary: dict[str, JsonValue] = {
            "finish_reason": result.finish_reason.value,
            "prompt_id": setup.prompt_id,
            "prompt_version": setup.prompt_version,
            "prompt_checksum": setup.prompt_checksum,
            "generated_characters": len(result.generated_text),
            "delivered_segments": len(result.delivered),
            "delivered_characters": len(result.delivered_text),
            "rejected_segments": list(result.rejected),
            "tail_discarded": result.tail_discarded,
            "late_events": result.late_events,
            "first_segment_ms": result.first_segment_ms,
            "tool_calls": 0,
            **_budget_evidence(budget),
        }
        if result.disclosure is not None:
            summary["disclosure_blocked"] = result.disclosure.value
        if result.provider_request_id is not None:
            summary["provider_request_id"] = result.provider_request_id
        return summary

    # ------------------------------------------------------------ finish --
    async def _finish(self, run: _TurnRun, result: GenerationResult) -> None:
        decision = resolve_completion(result)
        if decision.fallback is not None:
            spoken = await deliver_fallback(
                decision.fallback,
                fence=self._fence,
                turn_stamp=self._fence.stamp(turn_id=run.turn.turn_id),
                language=run.turn.language,
                deliver=self._fallback_sink(run),
            )
            if spoken:
                run.turn = run.turn.record_fallback()
        turn = run.turn.record_generated(result.generated_text).record_finish_reason(
            result.finish_reason
        )
        final = self._terminal_turn(turn, decision, superseded=result.cancelled)
        await self._evidence.save_turn(final)
        await self._publish_final(run, decision)
        await self._record_turn_event(final, decision)
        self._remember(run, result)
        if self._active is run and not self._closing:
            await self._publisher.publish_state(AgentActivityState.LISTENING)

    def _fallback_sink(self, run: _TurnRun) -> Callable[[DeliveredSegment], Awaitable[None]]:
        """The fallback reaches the browser only in the final message (no partial)."""

        async def sink(segment: DeliveredSegment) -> None:
            run.delivered.append(segment)

        return sink

    def _terminal_turn(
        self, turn: ConversationTurn, decision: CompletionDecision, *, superseded: bool
    ) -> ConversationTurn:
        if decision.outcome is TurnOutcome.COMPLETED:
            return turn.complete(decision.status, no_speakable_output=True)
        if decision.outcome is TurnOutcome.INTERRUPTED:
            reason = (
                InterruptionReason.USER_BARGE_IN
                if superseded
                else (InterruptionReason.SYSTEM_CANCEL)
            )
            if self._closing:
                reason = InterruptionReason.SESSION_END
            return turn.interrupt(reason=reason, phase=InterruptionPhase.THINKING)
        return turn.fail()

    async def _publish_final(self, run: _TurnRun, decision: CompletionDecision) -> None:
        fallback_id = None
        if decision.fallback is not None and run.turn.fallback_used:
            fallback_id = decision.fallback.template_id
        await self._publisher.publish_response_final(
            run.delivered_text(),
            turn_id=run.turn.turn_id,
            status=decision.status,
            fallback_template_id=fallback_id,
        )

    async def _record_turn_event(
        self, turn: ConversationTurn, decision: CompletionDecision
    ) -> None:
        event_type = {
            TurnOutcome.COMPLETED: EventType.TURN_COMPLETED,
            TurnOutcome.FAILED: EventType.TURN_FAILED,
            TurnOutcome.INTERRUPTED: EventType.TURN_INTERRUPTED,
        }[decision.outcome]
        await self._evidence.event(
            event_type,
            turn_id=turn.turn_id,
            payload={
                "response_completion_status": decision.status.value,
                "fallback_used": turn.fallback_used,
            },
        )

    def _remember(self, run: _TurnRun, result: GenerationResult) -> None:
        """History: the accepted transcript and only the delivered model text (docs/05 §13)."""
        turn_id = run.turn.turn_id
        transcript = run.turn.final_transcript or ""
        self.history.append(HistoryMessage(role=HistoryRole.USER, text=transcript, turn_id=turn_id))
        delivered = result.delivered_text
        if delivered.strip():
            self.history.append(
                HistoryMessage(role=HistoryRole.ASSISTANT, text=delivered, turn_id=turn_id)
            )

    async def _crashed(self, run: _TurnRun) -> None:
        with suppress(Exception):
            failed = run.turn.fail()
            await self._evidence.save_turn(failed)
            await self._evidence.event(
                EventType.TURN_FAILED, turn_id=failed.turn_id, severity=EventSeverity.ERROR
            )
        with suppress(Exception):
            await self._publisher.publish_error(
                "response_failed", "The response could not be generated.", retryable=True
            )

    # ------------------------------------------------------------- close --
    async def wait_idle(self) -> None:
        """Wait until every authorized generation has settled its turn."""
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        active = self._active
        self._fence.advance()
        if active is not None and active.operation_id is not None:
            await self._engine.cancel(active.operation_id)
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            with suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            task.cancel()
        await self._engine.close()


def _budget_evidence(budget: HistoryBudget) -> dict[str, JsonValue]:
    return {
        "history_messages": len(budget.messages),
        "history_dropped_turns": budget.dropped_turns,
        "estimated_input_tokens": budget.estimated_input_tokens,
    }
