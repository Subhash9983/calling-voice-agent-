"""Transcript-to-LLM executor over the real WP8 response pipeline (docs/17 §20).

Provider-independent: any :class:`ConversationEnginePort` (the mock engine,
the OpenAI adapter over a scripted fake, or — only after live approval — the
real OpenAI adapter) runs through the same generation fence, ResponseGenerator
(segmentation, speech normalization, disclosure guard), bounded retry policy,
and deterministic fallback as a live turn. Attempts are priced through the
WP11 ledger. Text is not spoken here; ``tts_submitted_response`` is the text
that would be submitted to TTS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from voice_agent.contracts.conversation import (
    MAX_OUTPUT_TOKENS,
    ConversationRequest,
    HistoryMessage,
    HistoryRole,
)
from voice_agent.contracts.cost import RateCard
from voice_agent.contracts.enums import (
    FinishReason,
    OperationComponent,
    OperationStatus,
    ResponseLanguage,
)
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.usage import UsageReport, UsageUnit
from voice_agent.costing.attempt_ledger import LedgerContext
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.evaluation.case import EvaluationCase, TranscriptInput
from voice_agent.domain.operation import ProviderOperation
from voice_agent.evaluation.cost_evidence import price_attempts
from voice_agent.evaluation.observations import HarnessError, TranscriptObservation
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.orchestration.history_budget import HistoryBudget, budget_history
from voice_agent.orchestration.response_generation import (
    Deliver,
    DeliveredSegment,
    GenerationResult,
    ResponseGenerator,
    deliver_fallback,
    resolve_completion,
    retry_decision,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.response_segmentation.disclosure import DisclosureGuard
from voice_agent.response_segmentation.language import classify_turn_language

GENERATE_RESPONSE: Final = "generate_response"
MS_PER_SECOND: Final = 1000
EngineFactory = Callable[[EvaluationCase, int], ConversationEnginePort]


@dataclass(frozen=True, slots=True)
class TranscriptHarnessSetup:
    agent_config_id: str
    config_checksum: str
    prompt_id: str
    prompt_version: int
    system_instruction: str
    provider: str
    model: str
    environment: AgentConfigEnvironment
    card: RateCard
    evidence_basis: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    correlation_id: str = "evaluation-transcript-harness"


@dataclass
class _Attempt:
    harness_id: str
    turn_id: str
    fence: GenerationFence
    operations: list[ProviderOperation] = field(default_factory=list)
    delivered: list[DeliveredSegment] = field(default_factory=list)


def _output_tokens(usage: UsageReport) -> int | None:
    for item in usage.items:
        if item.unit is UsageUnit.OUTPUT_TOKENS:
            return int(item.quantity)
    return None


class EngineTranscriptExecutor:
    def __init__(
        self,
        engine_factory: EngineFactory,
        setup: TranscriptHarnessSetup,
        *,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._factory = engine_factory
        self._setup = setup
        self._clock = clock
        self._ids = ids
        self._guard = DisclosureGuard.for_instruction(setup.system_instruction)

    @property
    def evidence_basis(self) -> str:
        return self._setup.evidence_basis

    @property
    def guard(self) -> DisclosureGuard:
        return self._guard

    async def execute(self, case: EvaluationCase, repetition: int) -> TranscriptObservation:
        if not isinstance(case.input, TranscriptInput):
            raise HarnessError("test_setup_invalid", "not a transcript case")
        engine = self._factory(case, repetition)
        try:
            return await self._run(case.input, engine)
        finally:
            await engine.close()

    def _history(self, data: TranscriptInput) -> list[HistoryMessage]:
        turn_id = self._ids.new_id()
        messages = []
        for turn in data.history_turns:
            if turn.role == "user":
                turn_id = self._ids.new_id()
            role = HistoryRole.USER if turn.role == "user" else HistoryRole.ASSISTANT
            messages.append(HistoryMessage(role=role, text=turn.text, turn_id=turn_id))
        return messages

    async def _run(
        self, data: TranscriptInput, engine: ConversationEnginePort
    ) -> TranscriptObservation:
        harness_id, turn_id = self._ids.new_id(), self._ids.new_id()
        attempt = _Attempt(
            harness_id, turn_id, GenerationFence(session_id=harness_id, worker_generation=1)
        )
        attempt.fence.activate_turn(turn_id)
        language = classify_turn_language(data.accepted_user_transcript, None)
        budget = budget_history(
            self._history(data),
            system_instruction=self._setup.system_instruction,
            user_transcript=data.accepted_user_transcript,
        )
        result = await self._generate(attempt, engine, data, budget, language)
        decision = resolve_completion(result)
        fallback_id = None
        if decision.fallback is not None:
            spoken = await deliver_fallback(
                decision.fallback,
                fence=attempt.fence,
                turn_stamp=attempt.fence.stamp(turn_id=turn_id),
                language=language,
                deliver=self._collector(attempt),
            )
            fallback_id = decision.fallback.template_id if spoken else None
        return self._observation(attempt, result, decision.status.value, fallback_id)

    def _collector(self, attempt: _Attempt) -> Deliver:
        async def deliver(segment: DeliveredSegment) -> None:
            attempt.delivered.append(segment)

        return deliver

    async def _generate(
        self,
        attempt: _Attempt,
        engine: ConversationEnginePort,
        data: TranscriptInput,
        budget: HistoryBudget,
        language: ResponseLanguage | None,
    ) -> GenerationResult:
        operation = self._new_operation(attempt)
        while True:
            result = await self._attempt(attempt, engine, operation, data, budget, language)
            decision = retry_decision(
                self._setup.retry, result, attempt_number=operation.attempt_number, jitter=0.0
            )
            if decision is None or not decision.should_retry:
                return result
            await asyncio.sleep((decision.backoff_ms or 0) / MS_PER_SECOND)
            operation = operation.next_attempt(self._ids.new_id())

    def _new_operation(self, attempt: _Attempt) -> ProviderOperation:
        return ProviderOperation(
            operation_id=self._ids.new_id(),
            logical_request_id=self._ids.new_id(),
            session_id=attempt.harness_id,
            turn_id=attempt.turn_id,
            component=OperationComponent.CONVERSATION_ENGINE,
            operation_type=GENERATE_RESPONSE,
            provider=self._setup.provider,
            model=self._setup.model,
            worker_generation=1,
        )

    async def _attempt(
        self,
        attempt: _Attempt,
        engine: ConversationEnginePort,
        operation: ProviderOperation,
        data: TranscriptInput,
        budget: HistoryBudget,
        language: ResponseLanguage | None,
    ) -> GenerationResult:
        setup = self._setup
        started = operation.transition_to(OperationStatus.STARTED, started_at=self._clock.utc_now())
        attempt.fence.register_operation(started.operation_id)
        request = ConversationRequest(
            stamp=attempt.fence.stamp(turn_id=attempt.turn_id, operation_id=started.operation_id),
            logical_request_id=started.logical_request_id,
            correlation_id=setup.correlation_id,
            agent_config_id=setup.agent_config_id,
            config_checksum=setup.config_checksum,
            system_instruction_id=setup.prompt_id,
            system_instruction_version=setup.prompt_version,
            system_instruction=setup.system_instruction,
            user_transcript=data.accepted_user_transcript,
            history=budget.messages,
            language=language,
            max_output_tokens=setup.max_output_tokens,
        )
        generator = ResponseGenerator(
            engine,
            fence=attempt.fence,
            guard=self._guard,
            deliver=self._collector(attempt),
            clock=self._clock,
        )
        try:
            result = await generator.run(request, language)
        finally:
            attempt.fence.retire_operation(started.operation_id)
        attempt.operations.append(_settled(started, result))
        return result

    def _observation(
        self, attempt: _Attempt, result: GenerationResult, status: str, fallback_id: str | None
    ) -> TranscriptObservation:
        setup = self._setup
        cost = price_attempts(
            attempt.operations,
            context=LedgerContext(
                session_id=attempt.harness_id,
                correlation_id=setup.correlation_id,
                agent_config_id=setup.agent_config_id,
                environment=setup.environment,
            ),
            card=setup.card,
            ids=self._ids,
            clock=self._clock,
        )
        failure = result.failure
        return TranscriptObservation(
            generated_text=result.generated_text,
            delivered_text=" ".join(segment.text for segment in attempt.delivered),
            finish_reason=result.finish_reason.value,
            completion_status=status,
            cost=cost,
            output_tokens=_output_tokens(result.usage),
            hit_token_limit=result.finish_reason is FinishReason.MAXIMUM_TOKENS,
            disclosure_blocked=None if result.disclosure is None else result.disclosure.value,
            fallback_template_id=fallback_id,
            rejected_segments=result.rejected,
            llm_first_token_ms=result.first_token_ms,
            llm_completion_ms=result.total_ms,
            attempts=len(attempt.operations),
            failure_type=None if failure is None else failure.error_type.value,
            engine_provider=setup.provider,
        )


def _settled(operation: ProviderOperation, result: GenerationResult) -> ProviderOperation:
    if result.cancelled:
        done = operation.cancel(result.usage)
    elif result.failure is not None:
        done = operation.fail(result.failure, result.usage)
    else:
        done = operation.succeed(result.usage)
    first_at = None
    if operation.started_at is not None and result.first_token_ms is not None:
        first_at = operation.started_at + timedelta(milliseconds=result.first_token_ms)
    return done.model_copy(
        update={
            "first_result_at": first_at,
            "time_to_first_result_ms": result.first_token_ms,
            "total_duration_ms": result.total_ms,
        }
    )
