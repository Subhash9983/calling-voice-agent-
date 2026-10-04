"""GPT-6 Luna adapter passes the conversation-port contract the mock passes (docs/08 §22)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import pytest
from tests.support.fake_openai import (
    PAUSE,
    FakeResponsesConnector,
    Step,
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
)
from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.failures import ErrorType
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.conversation_adapters.mock.adapter import (
    PAUSE_UNTIL_CANCELLED,
    MockConversationEngine,
    MockReply,
)
from voice_agent.conversation_adapters.openai.adapter import OpenAiConversationAdapter
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.ports.conversation import ConversationEnginePort

pytestmark = pytest.mark.asyncio
OPERATION = "00000000-0000-4000-8000-000000000003"
Scenario = str
EngineFactory = Callable[[Scenario], ConversationEnginePort]

_MOCK: dict[Scenario, MockReply] = {
    "complete": MockReply(steps=("Namaste! ", "Kaise ho?")),
    "pause": MockReply(steps=("One. ", PAUSE_UNTIL_CANCELLED, "Two.")),
    "fail": MockReply(steps=(), fail_with=ErrorType.PROVIDER_UNAVAILABLE),
    "truncate": MockReply(steps=("Cut o",), finish_reason=FinishReason.MAXIMUM_TOKENS),
}
_OPENAI: dict[Scenario, Sequence[Step]] = {
    "complete": reply("Namaste! ", "Kaise ho?"),
    "pause": [created(), delta("One. "), PAUSE, delta("Two.")],
    "fail": [created(), failed("server_error")],
    "truncate": reply("Cut o", finish=incomplete()),
}


def _mock(scenario: Scenario) -> ConversationEnginePort:
    return MockConversationEngine([_MOCK[scenario]])


def _openai(scenario: Scenario) -> ConversationEnginePort:
    return OpenAiConversationAdapter(
        FakeResponsesConnector([_OPENAI[scenario]]), clock=SystemClock()
    )


ENGINES = pytest.mark.parametrize("factory", [_mock, _openai], ids=["mock", "openai"])


def _request() -> ConversationRequest:
    session = "00000000-0000-4000-8000-000000000001"
    return ConversationRequest(
        stamp=GenerationStamp(
            session_id=session,
            turn_id="00000000-0000-4000-8000-000000000002",
            operation_id=OPERATION,
            worker_generation=1,
            cancellation_generation=0,
        ),
        logical_request_id=OPERATION,
        correlation_id="corr",
        agent_config_id=session,
        config_checksum="sha256:x",
        system_instruction_id="phase0_general_voice_assistant_v1",
        system_instruction_version=1,
        system_instruction="You are a friendly assistant.",
        user_transcript="Namaste",
    )


async def _collect(engine: ConversationEnginePort) -> list[ConversationEvent]:
    return [event async for event in engine.stream(_request())]


@ENGINES
async def test_satisfies_the_port_and_streams_then_completes(factory: EngineFactory) -> None:
    engine = factory("complete")
    assert isinstance(engine, ConversationEnginePort)

    events = await _collect(engine)

    assert isinstance(events[0], ConversationStarted)
    deltas = [e for e in events if isinstance(e, ConversationTextDelta)]
    assert [d.text for d in deltas] == ["Namaste! ", "Kaise ho?"]
    assert [d.sequence for d in deltas] == [0, 1]
    assert isinstance(events[-1], ConversationCompleted)
    assert events[-1].finish_reason is FinishReason.COMPLETED
    assert events[-1].usage.is_available
    assert all(e.stamp == _request().stamp for e in events)


@ENGINES
async def test_cancel_ends_the_stream_with_usage_evidence(factory: EngineFactory) -> None:
    engine = factory("pause")
    stream = engine.stream(_request())
    received = [await anext(stream), await anext(stream)]

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.01)
    await engine.cancel(OPERATION)
    received.append(await pending)
    await stream.aclose()  # type: ignore[attr-defined]

    assert isinstance(received[-1], ConversationCancelled)
    assert received[-1].usage.is_available


@ENGINES
async def test_failure_and_truncation_are_normalized(factory: EngineFactory) -> None:
    failed_event = (await _collect(factory("fail")))[-1]
    truncated = (await _collect(factory("truncate")))[-1]

    assert isinstance(failed_event, ConversationFailed)
    assert failed_event.failure.retryable
    assert failed_event.failure.error_type is ErrorType.PROVIDER_UNAVAILABLE
    assert isinstance(truncated, ConversationCompleted)
    assert truncated.finish_reason is FinishReason.MAXIMUM_TOKENS


@ENGINES
async def test_close_is_idempotent(factory: EngineFactory) -> None:
    engine = factory("complete")

    await engine.close()
    await engine.close()
