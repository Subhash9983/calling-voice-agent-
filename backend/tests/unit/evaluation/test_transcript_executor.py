"""The 60 transcript cases run offline through the real response pipeline and are scored.

Compliant fixture replies pass every deterministic assertion (semantic-only
checks stay ``unavailable``); each violating reply is caught by the named
assertion. Costs go through the WP11 ledger: the OpenAI adapter over a
scripted fake is priced and reconciled; the mock engine stays unpriced.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from tests.support.evaluation_replies import VIOLATIONS, ScriptedEngineFactory, case_number
from tests.support.fake_openai import FakeResponsesConnector, Step, failed, reply

from voice_agent.contracts.policies import RetryPolicy
from voice_agent.conversation_adapters.openai.adapter import OpenAiConversationAdapter
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import EvaluationEnvironment, EvaluationLayer
from voice_agent.evaluation.catalog import CatalogContext, build_catalog
from voice_agent.evaluation.observations import HarnessError
from voice_agent.evaluation.scoring import score_transcript
from voice_agent.evaluation.transcript_executor import (
    EngineFactory,
    EngineTranscriptExecutor,
    TranscriptHarnessSetup,
)
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION
from voice_agent.turn_management.fallbacks import RESPONSE_FAILED

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 6, tzinfo=UTC)
_, CASES = build_catalog(CatalogContext(EvaluationEnvironment.DEVELOPMENT, "t", NOW, "t"))
TRANSCRIPT = [c for c in CASES if c.layer is EvaluationLayer.TRANSCRIPT_LLM]
SEMANTIC_ONLY = {("txt-029", "b-context"), ("txt-059", "b-context"), ("txt-060", "b-context")}


def _setup(provider: str = "mock_llm", model: str = "mock-llm-v1") -> TranscriptHarnessSetup:
    return TranscriptHarnessSetup(
        agent_config_id="00000000-0000-4000-8000-00000000c9a1",
        config_checksum="sha256:" + "1" * 64,
        prompt_id="phase0_general_voice_assistant_v1",
        prompt_version=1,
        system_instruction=PHASE0_SYSTEM_INSTRUCTION,
        provider=provider,
        model=model,
        environment=AgentConfigEnvironment.DEVELOPMENT,
        card=phase0_rate_card(),
        evidence_basis="offline_fixture",
        retry=RetryPolicy(initial_backoff_ms=0, maximum_backoff_ms=0),
    )


def _executor(
    factory: EngineFactory, setup: TranscriptHarnessSetup | None = None
) -> EngineTranscriptExecutor:
    return EngineTranscriptExecutor(
        factory, setup or _setup(), clock=SystemClock(), ids=UuidIdGenerator()
    )


def _case(number: int) -> EvaluationCase:
    return next(c for c in TRANSCRIPT if case_number(c) == number)


async def test_all_60_compliant_replies_pass_every_deterministic_assertion() -> None:
    executor = _executor(ScriptedEngineFactory())
    not_passed = []
    for case in TRANSCRIPT:
        observed = await executor.execute(case, 1)
        scored = score_transcript(case, observed, guard=executor.guard, now=NOW)
        assert scored.critical_failures == ()
        assert scored.measurements.format_correct is True
        not_passed += [
            (case.case_key, a.assertion_id, a.outcome)
            for a in scored.assertion_results
            if a.outcome != "passed"
        ]

    assert {(key, aid) for key, aid, _ in not_passed} == SEMANTIC_ONLY
    assert {outcome for *_, outcome in not_passed} == {"unavailable"}


@pytest.mark.parametrize(
    ("number", "text", "assertion_id", "reason"),
    VIOLATIONS,
    ids=[f"txt-{v[0]:03d}-{v[2]}" for v in VIOLATIONS],
)
async def test_each_violation_is_detected(
    number: int, text: str, assertion_id: str, reason: str
) -> None:
    case = _case(number)
    executor = _executor(ScriptedEngineFactory({number: text}))

    scored = score_transcript(case, await executor.execute(case, 1), guard=executor.guard, now=NOW)
    failed_ids = {a.assertion_id: a for a in scored.assertion_results if a.outcome == "failed"}

    assert assertion_id in failed_ids
    assert failed_ids[assertion_id].reason_code == reason
    if failed_ids[assertion_id].critical:
        assert reason in scored.critical_failures


async def test_disclosure_is_blocked_before_delivery_and_still_fails_the_case() -> None:
    case = _case(42)
    executor = _executor(ScriptedEngineFactory({42: "The key is sk-abcdefghijklmnop1234."}))

    observed = await executor.execute(case, 1)
    scored = score_transcript(case, observed, guard=executor.guard, now=NOW)

    assert observed.disclosure_blocked == "credential_pattern"
    assert "sk-" not in observed.delivered_text
    assert observed.fallback_template_id == RESPONSE_FAILED.template_id
    assert "secret_disclosure" in scored.critical_failures


async def test_history_turns_reach_the_engine_request() -> None:
    factory = ScriptedEngineFactory()
    engines = []

    def capture(case: EvaluationCase, repetition: int):  # type: ignore[no-untyped-def]
        engine = factory(case, repetition)
        engines.append(engine)
        return engine

    await _executor(capture).execute(_case(58), 1)

    [request] = engines[0].requests
    assert [m.text for m in request.history][-1] == "समझ गया, आपका नाम आरुष है।"
    assert request.user_transcript == "मेरा नाम क्या है?"
    assert request.system_instruction == PHASE0_SYSTEM_INSTRUCTION


def _openai_factory(
    scripts: Sequence[Sequence[Step]],
) -> tuple[EngineFactory, list[FakeResponsesConnector]]:
    connectors: list[FakeResponsesConnector] = []

    def factory(_case: EvaluationCase, _repetition: int) -> OpenAiConversationAdapter:
        connector = FakeResponsesConnector(scripts)
        connectors.append(connector)
        return OpenAiConversationAdapter(connector, clock=SystemClock())

    return factory, connectors


async def test_openai_adapter_over_fake_stream_is_priced_and_reconciled() -> None:
    factory, connectors = _openai_factory([reply("The reference ID is SCH-09-A7X-204.")])
    executor = _executor(factory, _setup("openai", "gpt-6-luna"))

    observed = await executor.execute(_case(54), 1)

    assert connectors[0].params  # one Responses request, no network
    assert observed.cost.usage_status == "provider_reported"
    assert observed.cost.reconciliation_status == "matched"
    assert observed.cost.net_cost_usd is not None
    assert observed.cost.net_cost_usd > 0
    assert observed.cost.marginal_cost_inr is not None
    assert observed.output_tokens == 40


async def test_provider_failure_is_retried_within_policy_then_falls_back_once() -> None:
    broken = [failed("server_error")]
    factory, connectors = _openai_factory([broken, broken, broken])
    executor = _executor(factory, _setup("openai", "gpt-6-luna"))

    observed = await executor.execute(_case(3), 1)
    scored = score_transcript(_case(3), observed, guard=executor.guard, now=NOW)

    assert len(connectors[0].params) == 3
    assert observed.attempts == 3
    assert observed.fallback_template_id == RESPONSE_FAILED.template_id
    assert observed.failure_type is not None
    assert scored.status.value == "failed"
    assert scored.measurements.retry_count == 2
    assert scored.failure is not None


async def test_mock_engine_cost_stays_unpriced_never_zero() -> None:
    observed = await _executor(ScriptedEngineFactory()).execute(_case(3), 1)

    assert observed.cost.calculation_status == "unavailable"
    assert observed.cost.net_cost_usd is None
    assert observed.cost.unpriced[0][1] == "rate_unavailable"


async def test_a_non_transcript_case_is_a_harness_setup_error() -> None:
    live = next(c for c in CASES if c.layer is EvaluationLayer.LIVE_VOICE)

    with pytest.raises(HarnessError):
        await _executor(ScriptedEngineFactory()).execute(live, 1)
