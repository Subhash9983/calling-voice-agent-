"""Conversation-engine port contract (docs/08 §3, §22). WP8's adapter joins this suite."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationEvent,
    ConversationFailed,
    ConversationRequest,
    ConversationStarted,
    ConversationTextDelta,
)
from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.failures import ErrorType
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageUnit
from voice_agent.conversation_adapters.mock.adapter import (
    PAUSE_UNTIL_CANCELLED,
    MockConversationEngine,
    MockReply,
)
from voice_agent.ports.conversation import ConversationEnginePort

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
OPERATION = "00000000-0000-4000-8000-000000000003"


def _request() -> ConversationRequest:
    return ConversationRequest(
        stamp=GenerationStamp(
            session_id=SESSION,
            turn_id=TURN,
            operation_id=OPERATION,
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id=OPERATION,
        correlation_id="corr",
        agent_config_id=SESSION,
        config_checksum="sha256:x",
        system_instruction_id="phase0_general_voice_assistant_v1",
        system_instruction_version=1,
        system_instruction="You are a friendly assistant.",
        user_transcript="Namaste",
    )


async def _collect(engine: MockConversationEngine) -> list[ConversationEvent]:
    return [event async for event in engine.stream(_request())]


def test_engine_satisfies_the_port() -> None:
    assert isinstance(MockConversationEngine([]), ConversationEnginePort)


@pytest.mark.asyncio
async def test_stream_starts_streams_deltas_then_completes_with_usage() -> None:
    engine = MockConversationEngine([MockReply(steps=("Namaste! ", "Kaise ho?"))])

    events = await _collect(engine)

    assert isinstance(events[0], ConversationStarted)
    deltas = [e for e in events if isinstance(e, ConversationTextDelta)]
    assert [d.text for d in deltas] == ["Namaste! ", "Kaise ho?"]
    assert [d.sequence for d in deltas] == [0, 1]
    completed = events[-1]
    assert isinstance(completed, ConversationCompleted)
    assert completed.finish_reason is FinishReason.COMPLETED
    assert completed.usage.quantity_of(UsageUnit.OUTPUT_TOKENS) == 3
    assert all(e.stamp == _request().stamp for e in events)


@pytest.mark.asyncio
async def test_cancel_stops_a_cooperative_stream_with_usage() -> None:
    engine = MockConversationEngine([MockReply(steps=("One. ", PAUSE_UNTIL_CANCELLED, "Two."))])
    stream = engine.stream(_request())
    received: list[ConversationEvent] = [await anext(stream), await anext(stream)]

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await engine.cancel(OPERATION)
    received.append(await pending)

    assert isinstance(received[-1], ConversationCancelled)
    assert received[-1].usage.is_available
    await stream.aclose()


@pytest.mark.asyncio
async def test_uncooperative_stream_keeps_emitting_after_cancel() -> None:
    engine = MockConversationEngine(
        [MockReply(steps=("One. ", PAUSE_UNTIL_CANCELLED, "Late."), honor_cancel=False)]
    )
    stream = engine.stream(_request())
    await anext(stream)
    await anext(stream)

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await engine.cancel(OPERATION)
    late = await pending

    assert isinstance(late, ConversationTextDelta)
    assert late.text == "Late."
    await stream.aclose()


@pytest.mark.asyncio
async def test_failure_and_truncation_are_normalized() -> None:
    engine = MockConversationEngine(
        [
            MockReply(steps=("partial",), fail_with=ErrorType.PROVIDER_UNAVAILABLE),
            MockReply(steps=("Cut o",), finish_reason=FinishReason.MAXIMUM_TOKENS),
        ]
    )

    failed = (await _collect(engine))[-1]
    truncated = (await _collect(engine))[-1]

    assert isinstance(failed, ConversationFailed)
    assert failed.failure.retryable
    assert failed.failure.error_type is ErrorType.PROVIDER_UNAVAILABLE
    assert isinstance(truncated, ConversationCompleted)
    assert truncated.finish_reason is FinishReason.MAXIMUM_TOKENS


@pytest.mark.asyncio
async def test_missing_input_usage_stays_unavailable() -> None:
    engine = MockConversationEngine([MockReply(steps=("Hi.",), input_tokens=None)])

    completed = (await _collect(engine))[-1]

    assert isinstance(completed, ConversationCompleted)
    assert completed.usage.quantity_of(UsageUnit.INPUT_TOKENS) is None


@pytest.mark.asyncio
async def test_close_is_idempotent_and_exhausted_script_raises() -> None:
    engine = MockConversationEngine([])

    await engine.close()
    await engine.close()
    assert engine.closed
    with pytest.raises(RuntimeError, match="no scripted reply"):
        await _collect(engine)


@pytest.mark.asyncio
async def test_external_gate_step_waits_for_a_signal() -> None:
    gate = asyncio.Event()
    engine = MockConversationEngine([MockReply(steps=("A. ", gate, "B."))])
    collecting: asyncio.Task[Any] = asyncio.ensure_future(_collect(engine))
    await asyncio.sleep(0)
    assert not collecting.done()

    gate.set()
    events = await collecting

    assert [e.text for e in events if isinstance(e, ConversationTextDelta)] == ["A. ", "B."]
