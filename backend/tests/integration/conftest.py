"""Harness for the in-process mock vertical slice (docs/14 §8)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from voice_agent.contracts.cost import Currency, MeterKey, RateCard, UnitRate
from voice_agent.contracts.enums import SessionStatus
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.contracts.policies import QueueLimits, TurnHandlingPolicy
from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.contracts.usage import UsageUnit
from voice_agent.conversation_adapters.mock.adapter import (
    MOCK_CONVERSATION_MODEL,
    MOCK_CONVERSATION_PROVIDER,
    MockConversationEngine,
    MockReply,
)
from voice_agent.costing.rate_card import PLANNING_FX_INR_TO_USD
from voice_agent.domain.session import VoiceSession
from voice_agent.events_and_latency.clock import ManualClock, SequentialIdGenerator
from voice_agent.events_and_latency.writer import DurableEventWriter
from voice_agent.orchestration.runtime import (
    ConversationProfile,
    ProviderIdentity,
    SessionPorts,
    SessionSettings,
)
from voice_agent.orchestration.session_orchestrator import SessionOrchestrator
from voice_agent.persistence.in_memory import (
    InMemoryCostEntryRepository,
    InMemoryErrorEventRepository,
    InMemoryEventSequenceAllocator,
    InMemoryOperationRepository,
    InMemorySessionEventRepository,
    InMemorySessionRepository,
    InMemoryTurnRepository,
)
from voice_agent.speech_activity.mock import MockSpeechActivityDetector
from voice_agent.stt_adapters.mock.adapter import MOCK_STT_MODEL, MOCK_STT_PROVIDER, MockSttAdapter
from voice_agent.transport_adapters.mock.adapter import MockTransport, ScriptStep
from voice_agent.tts_adapters.mock.adapter import MOCK_TTS_MODEL, MOCK_TTS_PROVIDER, MockTtsAdapter

SESSION_ID = "00000000-0000-4000-8000-00000000aaaa"
CORRELATION_ID = "corr-wp2-mock-slice"
MOCK_RATE_CARD_ID = "wp2_mock_rate_card_v1"


def mock_rate_card() -> RateCard:
    """Test-only rates for mock providers (never real pricing)."""

    def rate(provider: str, model: str, unit: UsageUnit, value: str, per: int) -> UnitRate:
        return UnitRate(
            provider=provider,
            model=model,
            usage_unit=unit,
            billing_unit="unit",
            unit_rate=Decimal(value),
            rate_unit_quantity=Decimal(per),
            currency=Currency.INR,
        )

    return RateCard(
        rate_card_id=MOCK_RATE_CARD_ID,
        effective_date=date(2026, 9, 28),
        rates=(
            rate(MOCK_STT_PROVIDER, MOCK_STT_MODEL, UsageUnit.TRANSCRIBED_AUDIO_SECONDS, "1", 1),
            rate(
                MOCK_CONVERSATION_PROVIDER,
                MOCK_CONVERSATION_MODEL,
                UsageUnit.INPUT_TOKENS,
                "1",
                1000,
            ),
            rate(
                MOCK_CONVERSATION_PROVIDER,
                MOCK_CONVERSATION_MODEL,
                UsageUnit.CACHED_INPUT_TOKENS,
                "1",
                10_000,
            ),
            rate(
                MOCK_CONVERSATION_PROVIDER,
                MOCK_CONVERSATION_MODEL,
                UsageUnit.OUTPUT_TOKENS,
                "1",
                100,
            ),
            rate(MOCK_TTS_PROVIDER, MOCK_TTS_MODEL, UsageUnit.SYNTHESIZED_CHARACTERS, "1", 100),
        ),
        fx_rates=(PLANNING_FX_INR_TO_USD,),
        evidence_only=(
            MeterKey(
                provider=MOCK_TTS_PROVIDER,
                model=MOCK_TTS_MODEL,
                usage_unit=UsageUnit.GENERATED_AUDIO_SECONDS,
            ),
        ),
    )


@dataclass
class Harness:
    log: list[str]
    transport: MockTransport
    stt: MockSttAdapter
    conversation: MockConversationEngine
    tts: MockTtsAdapter
    writer: DurableEventWriter
    events: InMemorySessionEventRepository
    turns: InMemoryTurnRepository
    errors: InMemoryErrorEventRepository
    costs: InMemoryCostEntryRepository
    ports: SessionPorts
    settings: SessionSettings
    orchestrator: SessionOrchestrator

    def event_types(self) -> list[EventType]:
        return [event.event_type for event in self.writer.timeline]

    async def durable(self) -> Sequence[EventEnvelope]:
        return await self.events.list_for_session(SESSION_ID)


HarnessFactory = Callable[..., Harness]


def _settings(*, clarification_fallback: bool) -> SessionSettings:
    return SessionSettings(
        worker_generation=1,
        turn_handling=TurnHandlingPolicy(),
        queue_limits=QueueLimits(),
        stt_config=SttStreamConfig(),
        voice=TtsVoiceConfig(provider=MOCK_TTS_PROVIDER, model=MOCK_TTS_MODEL, voice_id="mock"),
        conversation=ConversationProfile(
            agent_config_id="00000000-0000-4000-8000-00000000c0f1",
            config_checksum="sha256:mock",
            system_instruction_id="phase0_general_voice_assistant_v1",
            system_instruction_version=1,
            system_instruction="You are a friendly general-purpose AI voice assistant.",
        ),
        stt_identity=ProviderIdentity(MOCK_STT_PROVIDER, MOCK_STT_MODEL),
        conversation_identity=ProviderIdentity(MOCK_CONVERSATION_PROVIDER, MOCK_CONVERSATION_MODEL),
        tts_identity=ProviderIdentity(MOCK_TTS_PROVIDER, MOCK_TTS_MODEL),
        rate_card=mock_rate_card(),
        reporting_currency=Currency.USD,
        clarification_fallback_enabled=clarification_fallback,
    )


def _tts(log: list[str], options: dict[str, Any]) -> MockTtsAdapter:
    tts = options.get("tts")
    if tts is not None:
        return tts  # type: ignore[no-any-return]
    kwargs: dict[str, Any] = {
        "honor_cancel": options.get("tts_honor_cancel", True),
        "record": log.append,
    }
    for key, target in (("tts_pause_when", "pause_when"), ("tts_fail_when", "fail_when")):
        if options.get(key) is not None:
            kwargs[target] = options[key]
    return MockTtsAdapter(**kwargs)


@pytest.fixture
def build_harness() -> HarnessFactory:
    def build(
        *,
        script: Sequence[ScriptStep],
        transcripts: Sequence[str],
        replies: Sequence[MockReply],
        **options: Any,
    ) -> Harness:
        log: list[str] = []
        transport_class = options.get("transport_class", MockTransport)
        transport = transport_class(
            SESSION_ID,
            script,
            yield_per_frame=options.get("yield_per_frame", True),
            playback_failure=options.get("playback_failure", False),
            record=log.append,
        )
        stt = options.get("stt") or MockSttAdapter(transcripts, record=log.append)
        conversation = MockConversationEngine(replies, record=log.append)
        tts = _tts(log, options)
        events = InMemorySessionEventRepository()
        writer = DurableEventWriter(allocator=InMemoryEventSequenceAllocator(), repository=events)
        turns = InMemoryTurnRepository(record=log.append)
        costs = InMemoryCostEntryRepository()
        errors = InMemoryErrorEventRepository()
        ports = SessionPorts(
            transport=transport,
            speech_activity=MockSpeechActivityDetector(TurnHandlingPolicy()),
            stt=stt,
            conversation=conversation,
            tts=tts,
            events=writer,
            sessions=InMemorySessionRepository(),
            turns=turns,
            operations=InMemoryOperationRepository(),
            cost_entries=costs,
            errors=errors,
            clock=ManualClock(),
            ids=SequentialIdGenerator(start=1000),
        )
        settings = _settings(clarification_fallback=options.get("clarification_fallback", False))
        session = VoiceSession(
            session_id=SESSION_ID, correlation_id=CORRELATION_ID, status=SessionStatus.CONNECTING
        )
        orchestrator = SessionOrchestrator(session=session, ports=ports, settings=settings)
        orchestrator.fence.add_observer(lambda gen: log.append(f"fence.advanced:{gen}"))
        return Harness(
            log=log,
            transport=transport,
            stt=stt,
            conversation=conversation,
            tts=tts,
            writer=writer,
            events=events,
            turns=turns,
            errors=errors,
            costs=costs,
            ports=ports,
            settings=settings,
            orchestrator=orchestrator,
        )

    return build
