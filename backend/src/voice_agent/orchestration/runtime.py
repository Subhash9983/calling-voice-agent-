"""Orchestrator inputs: ports, immutable settings, and the session report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from voice_agent.contracts.conversation import MAX_OUTPUT_TOKENS, HistoryMessage
from voice_agent.contracts.cost import (
    NORMALIZED_COST_CURRENCY,
    CostCalculation,
    Currency,
    RateCard,
)
from voice_agent.contracts.policies import QueueLimits, TurnHandlingPolicy
from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.ports.events import EventPublisher
from voice_agent.ports.repositories import (
    CostEntryRepository,
    ErrorEventRepository,
    OperationRepository,
    SessionRepository,
    TurnRepository,
)
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.ports.stt import STTPort
from voice_agent.ports.transport import WorkerTransportPort
from voice_agent.ports.tts import TTSPort

DEFAULT_INBOX_CAPACITY = 512


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider: str
    model: str | None


@dataclass(frozen=True, slots=True)
class ConversationProfile:
    agent_config_id: str
    config_checksum: str
    system_instruction_id: str
    system_instruction_version: int
    system_instruction: str
    max_output_tokens: int = MAX_OUTPUT_TOKENS


@dataclass(frozen=True, slots=True)
class SessionPorts:
    transport: WorkerTransportPort
    speech_activity: SpeechActivityPort
    stt: STTPort
    conversation: ConversationEnginePort
    tts: TTSPort
    events: EventPublisher
    sessions: SessionRepository
    turns: TurnRepository
    operations: OperationRepository
    cost_entries: CostEntryRepository
    errors: ErrorEventRepository
    clock: Clock
    ids: IdGenerator


@dataclass(frozen=True, slots=True)
class SessionSettings:
    worker_generation: int
    turn_handling: TurnHandlingPolicy
    queue_limits: QueueLimits
    stt_config: SttStreamConfig
    voice: TtsVoiceConfig
    conversation: ConversationProfile
    stt_identity: ProviderIdentity
    conversation_identity: ProviderIdentity
    tts_identity: ProviderIdentity
    rate_card: RateCard
    reporting_currency: Currency = NORMALIZED_COST_CURRENCY
    clarification_fallback_enabled: bool = False
    inbox_capacity: int = DEFAULT_INBOX_CAPACITY


@dataclass(frozen=True, slots=True)
class SessionReport:
    turns: tuple[ConversationTurn, ...]
    history: tuple[HistoryMessage, ...]
    cost: CostCalculation | None
    late_discards: Mapping[str, int]
    ignored_acks: int
