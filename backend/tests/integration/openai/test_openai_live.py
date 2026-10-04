"""Opt-in live GPT-6 Luna checks (``-m openai``; metered; WP8 budget USD 0.25 in docs/15).

Gated three ways by ``tests/conftest.py``: the ``openai`` marker must be
selected explicitly, ``VOICE_AGENT_SECRETS_FILE`` must be set, and
``VOICE_AGENT_OPENAI_LIVE_APPROVED=1`` must be set (only after the docs/15
budget is recorded). Synthetic text only; the key loads only through the WP3
loader/resolver and is never printed. Each billable call sends the ~3.3k
character approved instruction plus one short sentence and is guarded by
:data:`tests.support.openai_probe.SPEND` (worst case about USD 0.002/call).

Printed evidence (``-s``) is structural: event type names, terminal
status/``incomplete_details``, the usage key tree with numbers, timings, and
cost. Response text is synthetic and printed only for language review.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.openai_probe import SPEND, RecordingConnector, live_settings

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationEvent,
    ConversationFailed,
    ConversationRequest,
    ConversationTextDelta,
)
from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.failures import ErrorType
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageReportingStatus, UsageUnit
from voice_agent.conversation_adapters.openai.adapter import OpenAiConversationAdapter
from voice_agent.conversation_adapters.openai.sdk_binding import SdkResponsesConnector
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.provider_registry.llm_check_config import OPENAI_CREDENTIAL_REF
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION
from voice_agent.security.credentials import CredentialError, CredentialResolver

pytestmark = [pytest.mark.openai, pytest.mark.asyncio]
SESSION = "00000000-0000-4000-8000-0000000008a1"
TURN = "00000000-0000-4000-8000-0000000008a2"
OPERATION = "00000000-0000-4000-8000-0000000008a3"


def _api_key() -> SecretStr:
    settings = live_settings()
    try:
        return CredentialResolver(settings).resolve(OPENAI_CREDENTIAL_REF)
    except CredentialError:
        pytest.skip("OPENAI_API_KEY is not configured in the secrets file")


def _request(text: str, max_output_tokens: int = 250) -> ConversationRequest:
    return ConversationRequest(
        stamp=GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id=OPERATION,
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id=OPERATION,
        correlation_id="wp8-live",
        agent_config_id=SESSION,
        config_checksum="sha256:live",
        system_instruction_id="phase0_general_voice_assistant_v1",
        system_instruction_version=1,
        system_instruction=PHASE0_SYSTEM_INSTRUCTION,
        user_transcript=text,
        max_output_tokens=max_output_tokens,
    )


def _adapter() -> tuple[OpenAiConversationAdapter, RecordingConnector]:
    connector = RecordingConnector(SdkResponsesConnector(_api_key(), timeout_s=50.0))
    return OpenAiConversationAdapter(connector, clock=SystemClock()), connector


async def _timed(
    adapter: OpenAiConversationAdapter, request: ConversationRequest
) -> tuple[list[ConversationEvent], int | None, int]:
    SPEND.before_call()
    started = time.perf_counter()
    first: int | None = None
    events: list[ConversationEvent] = []
    async for event in adapter.stream(request):
        if isinstance(event, ConversationTextDelta) and first is None:
            first = round((time.perf_counter() - started) * 1000)
        events.append(event)
    return events, first, round((time.perf_counter() - started) * 1000)


def _report(name: str, **fields: Any) -> None:
    print(f"\nWP8-LIVE {name} " + json.dumps(fields, ensure_ascii=True, default=str))


async def test_live_stream_returns_text_provider_usage_and_sse_shapes() -> None:
    adapter, connector = _adapter()
    try:
        events, first_ms, total_ms = await _timed(adapter, _request("Namaste, aap kaun ho?"))
    finally:
        await adapter.close()

    done = events[-1]
    assert isinstance(done, ConversationCompleted)
    cost = SPEND.add(done.usage)
    text = "".join(e.text for e in events if isinstance(e, ConversationTextDelta))
    _report(
        "stream",
        first_token_ms=first_ms,
        total_ms=total_ms,
        finish=done.finish_reason.value,
        request_id_present=done.provider_request_id is not None,
        usage={item.unit.value: str(item.quantity) for item in done.usage.items},
        cost_usd=str(cost),
        shape=connector.shapes[0].__dict__,
        text=text,
    )
    assert text
    assert done.finish_reason is FinishReason.COMPLETED
    assert done.usage.reporting_status is UsageReportingStatus.PROVIDER_REPORTED
    assert done.usage.quantity_of(UsageUnit.INPUT_TOKENS) is not None
    assert connector.shapes[0].terminal["store"] is False


async def test_live_output_cap_maps_incomplete_to_maximum_tokens() -> None:
    adapter, connector = _adapter()
    prompt = "Please explain the full history of Indian railways in great detail."
    try:
        events, first_ms, total_ms = await _timed(adapter, _request(prompt, max_output_tokens=16))
    finally:
        await adapter.close()

    done = events[-1]
    assert isinstance(done, ConversationCompleted)
    cost = SPEND.add(done.usage)
    _report(
        "output_cap",
        first_token_ms=first_ms,
        total_ms=total_ms,
        finish=done.finish_reason.value,
        terminal=connector.shapes[0].terminal,
        cost_usd=str(cost),
    )
    assert done.finish_reason is FinishReason.MAXIMUM_TOKENS
    assert connector.shapes[0].terminal["incomplete_details"] == {"reason": "max_output_tokens"}
    output = done.usage.quantity_of(UsageUnit.OUTPUT_TOKENS)
    assert output is not None
    assert output <= Decimal(16)


async def test_live_cancel_after_first_token_stops_the_stream() -> None:
    adapter, _ = _adapter()
    prompt = "Explain in English, step by step, how a train timetable is planned."
    SPEND.before_call()
    stream = adapter.stream(_request(prompt))
    late: list[ConversationEvent] = []
    started = time.perf_counter()
    try:
        async for event in stream:
            if isinstance(event, ConversationTextDelta):
                break
        cancel_at = time.perf_counter()
        await adapter.cancel(OPERATION)
        async for event in stream:
            late.append(event)
        ack_ms = round((time.perf_counter() - cancel_at) * 1000)
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
        await adapter.close()

    cancelled = late[-1]
    assert isinstance(cancelled, ConversationCancelled)
    cost = SPEND.add(cancelled.usage)
    _report(
        "cancel",
        first_token_ms=round((cancel_at - started) * 1000),
        cancel_ack_ms=ack_ms,
        events_after_cancel=[type(e).__name__ for e in late],
        usage_status=cancelled.usage.reporting_status.value,
        estimated_cost_usd=str(cost),
    )
    assert not any(isinstance(e, ConversationTextDelta) for e in late)
    assert ack_ms < 1000


async def test_live_rejected_credential_is_normalized() -> None:
    adapter = OpenAiConversationAdapter(
        SdkResponsesConnector(SecretStr("sk-invalid-wp8-check"), timeout_s=20.0),
        clock=SystemClock(),
    )
    try:
        events = [e async for e in adapter.stream(_request("Hello"))]
    finally:
        await adapter.close()

    done = events[-1]
    assert isinstance(done, ConversationFailed)
    _report("rejected_key", error=done.failure.error_type.value, status=done.failure.status_code)
    assert done.failure.error_type is ErrorType.AUTHENTICATION_FAILED
    assert not done.failure.retryable


async def test_live_spend_summary() -> None:
    await asyncio.sleep(0)
    _report("spend", calls=SPEND.calls, spent_usd=str(SPEND.spent_usd))
    assert SPEND.spent_usd < Decimal("0.05")
