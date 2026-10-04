"""OpenAI GPT-6 Luna adapter over a scripted fake Responses stream (WP8, offline)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

import pytest
from tests.support.fake_openai import (
    PAUSE,
    RESPONSE_ID,
    FakeResponsesConnector,
    Step,
    completed,
    created,
    delta,
    failed,
    incomplete,
    reply,
)

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationEvent,
    ConversationFailed,
    ConversationRequest,
    ConversationStarted,
    ConversationTextDelta,
    HistoryMessage,
    HistoryRole,
)
from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.failures import ErrorType
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageReportingStatus, UsageUnit
from voice_agent.conversation_adapters.openai.adapter import (
    OpenAiConversationAdapter,
    OpenAiTimeouts,
)
from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
)
from voice_agent.conversation_adapters.openai.options import request_params
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION

pytestmark = pytest.mark.asyncio
SESSION = "00000000-0000-4000-8000-000000000801"
TURN = "00000000-0000-4000-8000-000000000802"
OPERATION = "00000000-0000-4000-8000-000000000803"
EARLIER_TURN = "00000000-0000-4000-8000-000000000804"


def _request(
    transcript: str = "Namaste, aap kaise ho?", **overrides: object
) -> ConversationRequest:
    fields: dict[str, object] = {
        "stamp": GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id=OPERATION,
            worker_generation=1,
            cancellation_generation=0,
        ),
        "logical_request_id": OPERATION,
        "correlation_id": "corr-wp8",
        "agent_config_id": SESSION,
        "config_checksum": "sha256:x",
        "system_instruction_id": "phase0_general_voice_assistant_v1",
        "system_instruction_version": 1,
        "system_instruction": PHASE0_SYSTEM_INSTRUCTION,
        "user_transcript": transcript,
        **overrides,
    }
    return ConversationRequest.model_validate(fields)


def _adapter(
    scripts: Sequence[Sequence[Step]], **kwargs: object
) -> tuple[OpenAiConversationAdapter, FakeResponsesConnector]:
    connector = FakeResponsesConnector(scripts, **kwargs)  # type: ignore[arg-type]
    return OpenAiConversationAdapter(connector, clock=SystemClock()), connector


async def _collect(
    adapter: OpenAiConversationAdapter, request: ConversationRequest | None = None
) -> list[ConversationEvent]:
    return [event async for event in adapter.stream(request or _request())]


def _texts(events: list[ConversationEvent]) -> list[str]:
    return [e.text for e in events if isinstance(e, ConversationTextDelta)]


async def test_adapter_satisfies_the_conversation_port() -> None:
    adapter, _ = _adapter([])
    assert isinstance(adapter, ConversationEnginePort)
    assert repr(adapter) == "OpenAiConversationAdapter(model='gpt-6-luna')"


async def test_request_is_the_exact_instruction_bounded_history_then_current_turn() -> None:
    history = (
        HistoryMessage(role=HistoryRole.USER, text="Mera naam Arun hai.", turn_id=EARLIER_TURN),
        HistoryMessage(role=HistoryRole.ASSISTANT, text="Namaste Arun!", turn_id=EARLIER_TURN),
    )
    params = request_params(_request("Aaj kya plan hai?", history=history))

    assert params["instructions"] == PHASE0_SYSTEM_INSTRUCTION
    assert params["input"] == [
        {"role": "user", "content": "Mera naam Arun hai."},
        {"role": "assistant", "content": "Namaste Arun!"},
        {"role": "user", "content": "Aaj kya plan hai?"},
    ]
    assert params["reasoning"] == {"effort": "none"}
    assert params["store"] is False
    assert params["stream"] is True
    assert params["max_output_tokens"] == 250
    assert params["service_tier"] == "default"
    forbidden = {"tools", "tool_choice", "temperature", "top_p", "previous_response_id"}
    assert forbidden.isdisjoint(params)
    assert {"conversation", "prompt", "metadata", "include", "background"}.isdisjoint(params)


@pytest.mark.parametrize(
    ("transcript", "chunks"),
    [
        ("आज मौसम कैसा है?", ("मेरे पास live मौसम ", "की जानकारी नहीं है।")),
        ("Kal meeting kitne baje hai?", ("Mujhe aapka calendar ", "nahi dikhta.")),
        ("What can you do?", ("I can chat in Hindi, ", "Hinglish, and English.")),
    ],
)
async def test_streams_ordered_deltas_then_completes_with_provider_usage(
    transcript: str, chunks: tuple[str, ...]
) -> None:
    adapter, connector = _adapter([reply(*chunks, finish=completed(input_tokens=2600, cached=600))])

    events = await _collect(adapter, _request(transcript))

    assert isinstance(events[0], ConversationStarted)
    assert _texts(events) == list(chunks)
    assert [e.sequence for e in events if isinstance(e, ConversationTextDelta)] == [0, 1]
    done = events[-1]
    assert isinstance(done, ConversationCompleted)
    assert done.finish_reason is FinishReason.COMPLETED
    assert done.provider_request_id == RESPONSE_ID
    assert done.usage.reporting_status is UsageReportingStatus.PROVIDER_REPORTED
    assert done.usage.quantity_of(UsageUnit.INPUT_TOKENS) == Decimal(2000)
    assert done.usage.quantity_of(UsageUnit.CACHED_INPUT_TOKENS) == Decimal(600)
    assert done.usage.quantity_of(UsageUnit.OUTPUT_TOKENS) == Decimal(40)
    assert done.usage.quantity_of(UsageUnit.CACHE_WRITE_TOKENS) is None
    assert connector.params[0]["input"][-1] == {"role": "user", "content": transcript}
    assert connector.closed_streams == 1


async def test_cache_writes_are_a_separate_meter_removed_from_uncached_input() -> None:
    finish = completed(input_tokens=3000, cached=1000, cache_write=500, reasoning=0)
    adapter, _ = _adapter([reply("Theek hai.", finish=finish)])

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationCompleted)
    assert done.usage.quantity_of(UsageUnit.INPUT_TOKENS) == Decimal(1500)
    assert done.usage.quantity_of(UsageUnit.CACHED_INPUT_TOKENS) == Decimal(1000)
    assert done.usage.quantity_of(UsageUnit.CACHE_WRITE_TOKENS) == Decimal(500)
    assert done.usage.quantity_of(UsageUnit.REASONING_TOKENS) == Decimal(0)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("max_output_tokens", FinishReason.MAXIMUM_TOKENS),
        ("content_filter", FinishReason.CONTENT_FILTERED),
        ("something_new", FinishReason.MAXIMUM_TOKENS),
    ],
)
async def test_incomplete_responses_map_to_normalized_finish_reasons(
    reason: str, expected: FinishReason
) -> None:
    adapter, _ = _adapter([reply("Pehla vaakya. Dusra adh", finish=incomplete(reason))])

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationCompleted)
    assert done.finish_reason is expected


@pytest.mark.parametrize(
    ("code", "error_type", "retryable"),
    [
        ("server_error", ErrorType.PROVIDER_UNAVAILABLE, True),
        ("rate_limit_exceeded", ErrorType.RATE_LIMITED, True),
        ("insufficient_quota", ErrorType.QUOTA_EXHAUSTED, False),
        ("context_length_exceeded", ErrorType.CONFIGURATION_INVALID, False),
        ("content_filter", ErrorType.CONTENT_REJECTED, False),
        ("surprising_code", ErrorType.UNKNOWN_PROVIDER_ERROR, False),
    ],
)
async def test_provider_failures_are_normalized_without_provider_text(
    code: str, error_type: ErrorType, retryable: bool
) -> None:
    adapter, _ = _adapter([[created(), failed(code)]])

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationFailed)
    assert done.failure.error_type is error_type
    assert done.failure.retryable is retryable
    assert "provider detail" not in done.failure.safe_message
    assert done.failure.failure_phase == "stream"
    # Accepted by the provider but no usage returned: estimated, never zero.
    assert done.usage.reporting_status is UsageReportingStatus.ESTIMATED


async def test_error_event_is_a_failure() -> None:
    adapter, _ = _adapter([[created(), {"type": "error", "code": "rate_limit_exceeded"}]])

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationFailed)
    assert done.failure.error_type is ErrorType.RATE_LIMITED


@pytest.mark.parametrize(
    ("kind", "status", "error_type"),
    [
        (OpenAiErrorKind.AUTHENTICATION, 401, ErrorType.AUTHENTICATION_FAILED),
        (OpenAiErrorKind.PERMISSION, 403, ErrorType.PERMISSION_DENIED),
        (OpenAiErrorKind.CONFIGURATION, 400, ErrorType.CONFIGURATION_INVALID),
        (OpenAiErrorKind.CONNECT_FAILED, None, ErrorType.CONNECTION_FAILED),
    ],
)
async def test_request_rejection_has_unavailable_usage(
    kind: OpenAiErrorKind, status: int | None, error_type: ErrorType
) -> None:
    error = OpenAiTransportError(kind, status_code=status, request_id="req_safe")
    adapter, _ = _adapter([], open_error=error)

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationFailed)
    assert done.failure.error_type is error_type
    assert done.failure.status_code == status
    assert done.failure.failure_phase == "request"
    assert done.usage.reporting_status is UsageReportingStatus.UNAVAILABLE


async def test_mid_stream_transport_loss_is_retryable_with_estimated_usage() -> None:
    lost = OpenAiTransportError(OpenAiErrorKind.CONNECTION_LOST)
    adapter, _ = _adapter([[created(), delta("Ek second "), lost]])

    events = await _collect(adapter)

    done = events[-1]
    assert isinstance(done, ConversationFailed)
    assert done.failure.error_type is ErrorType.CONNECTION_LOST
    assert done.failure.retryable
    assert done.failure.failure_phase == "stream"
    assert done.usage.quantity_of(UsageUnit.OUTPUT_TOKENS) is not None


async def test_stream_ending_without_a_terminal_event_is_a_lost_stream() -> None:
    adapter, _ = _adapter([[created(), delta("Adhoora")]])

    done = (await _collect(adapter))[-1]

    assert isinstance(done, ConversationFailed)
    assert done.failure.error_type is ErrorType.CONNECTION_LOST
    assert done.failure.failure_phase == "stream_ended"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_item.added", "item": {"type": "function_call"}},
        {"type": "response.web_search_call.in_progress"},
        {"type": "response.mcp_list_tools.in_progress"},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
    ],
)
async def test_any_tool_activity_fails_the_attempt(event: dict[str, object]) -> None:
    adapter, _ = _adapter([[created(), event, completed()]])

    events = await _collect(adapter)

    assert isinstance(events[-1], ConversationFailed)
    assert events[-1].failure.error_type is ErrorType.CONFIGURATION_INVALID
    assert _texts(events) == []


async def test_reasoning_refusal_and_unknown_events_never_escape() -> None:
    noise: list[Step] = [
        created(),
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.reasoning_text.delta", "delta": "hidden reasoning"},
        {"type": "response.refusal.delta", "delta": "refusal text"},
        {"type": "response.brand_new_event"},
        {"no_type": True},
        {"type": "response.output_text.delta", "delta": 7},
        delta(""),
        delta("Sirf yeh bolna hai."),
        completed(),
    ]
    adapter, _ = _adapter([noise])

    events = await _collect(adapter)

    assert _texts(events) == ["Sirf yeh bolna hai."]
    assert all("hidden" not in getattr(e, "text", "") for e in events)
    assert isinstance(events[-1], ConversationCompleted)


async def test_cancel_closes_the_stream_with_estimated_usage() -> None:
    adapter, connector = _adapter([[created(), delta("Pehla hissa. "), PAUSE, delta("Late.")]])
    stream = adapter.stream(_request())
    received = [await anext(stream), await anext(stream)]

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.01)
    await adapter.cancel(OPERATION)
    received.append(await pending)
    await stream.aclose()

    cancelled = received[-1]
    assert isinstance(cancelled, ConversationCancelled)
    assert cancelled.usage.reporting_status is UsageReportingStatus.ESTIMATED
    assert cancelled.usage.quantity_of(UsageUnit.OUTPUT_TOKENS) == Decimal(7)
    assert connector.closed_streams == 1


async def test_cancel_before_start_never_reads_the_stream() -> None:
    adapter, _ = _adapter([reply("Should not appear.")])

    await adapter.cancel(OPERATION)
    events = await _collect(adapter)

    assert _texts(events) == []
    assert isinstance(events[-1], ConversationCancelled)


async def test_first_token_and_total_deadlines_are_non_retryable_timeouts() -> None:
    connector = FakeResponsesConnector([[created(), PAUSE], [created(), delta("Haan, "), PAUSE]])
    adapter = OpenAiConversationAdapter(
        connector, clock=SystemClock(), timeouts=OpenAiTimeouts(first_token_ms=30, total_ms=60)
    )

    first = (await _collect(adapter))[-1]
    total = (await _collect(adapter))[-1]

    assert isinstance(first, ConversationFailed)
    assert first.failure.error_type is ErrorType.PROVIDER_TIMEOUT
    assert first.failure.failure_phase == "first_token_timeout"
    assert not first.failure.retryable
    assert isinstance(total, ConversationFailed)
    assert total.failure.failure_phase == "total_timeout"


async def test_output_beyond_the_contract_bound_fails() -> None:
    adapter, _ = _adapter([[created(), delta("a" * 15_000), delta("b" * 6000), completed()]])

    events = await _collect(adapter)

    assert isinstance(events[-1], ConversationFailed)
    assert events[-1].failure.failure_phase == "output_bound"


async def test_close_is_idempotent_and_later_streams_are_cancelled() -> None:
    adapter, connector = _adapter([reply("Never.")])

    await adapter.close()
    await adapter.close()
    events = await _collect(adapter)

    assert connector.aclosed
    assert connector.params == []
    assert isinstance(events[-1], ConversationCancelled)
    assert events[-1].usage.reporting_status is UsageReportingStatus.UNAVAILABLE
