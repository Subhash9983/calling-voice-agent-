"""Conversation-engine port inputs and normalized outputs (docs/01 §13, docs/08 §4-§12).

Provider message classes never enter conversation-core history; the
orchestrator constructs normalized history from accepted user transcripts
and delivered assistant content only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    ShortLabel,
    StrictModel,
)
from voice_agent.contracts.enums import FinishReason, ResponseLanguage
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageReport

MAX_OUTPUT_TOKENS = 250
MAX_SYSTEM_INSTRUCTION_CHARS = 50_000
MAX_USER_TRANSCRIPT_CHARS = 10_000
MAX_GENERATED_TEXT_CHARS = 20_000
MAX_HISTORY_MESSAGES = 200


class HistoryRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class HistoryMessage(StrictModel):
    """One accepted user transcript or delivered assistant text (docs/08 §7)."""

    role: HistoryRole
    text: Annotated[str, Field(min_length=1, max_length=MAX_GENERATED_TEXT_CHARS)]
    turn_id: CanonicalId

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("history text cannot be blank")
        return value


class ConversationRequest(StrictModel):
    """Every generation request (docs/08 §4). Phase 0 tools are always empty."""

    stamp: GenerationStamp
    logical_request_id: CanonicalId
    correlation_id: ExternalIdentifier
    agent_config_id: CanonicalId
    config_checksum: ExternalIdentifier
    system_instruction_id: ShortLabel
    system_instruction_version: Annotated[int, Field(ge=1)]
    system_instruction: Annotated[str, Field(min_length=1, max_length=MAX_SYSTEM_INSTRUCTION_CHARS)]
    user_transcript: Annotated[str, Field(min_length=1, max_length=MAX_USER_TRANSCRIPT_CHARS)]
    history: Annotated[tuple[HistoryMessage, ...], Field(max_length=MAX_HISTORY_MESSAGES)] = ()
    language: ResponseLanguage | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=MAX_OUTPUT_TOKENS)] = MAX_OUTPUT_TOKENS
    tool_set_version: ShortLabel = "phase0_empty_v1"
    tools: tuple[()] = ()

    @field_validator("user_transcript")
    @classmethod
    def _usable_transcript(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an empty transcript cannot authorize a conversation request")
        return value

    @model_validator(mode="after")
    def _operation_identity(self) -> ConversationRequest:
        if self.stamp.turn_id is None or self.stamp.operation_id is None:
            raise ValueError("conversation requests require turn and operation identity")
        return self


class ConversationStarted(StrictModel):
    kind: Literal["started"] = "started"
    stamp: GenerationStamp


class ConversationTextDelta(StrictModel):
    kind: Literal["text_delta"] = "text_delta"
    stamp: GenerationStamp
    sequence: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(min_length=1, max_length=MAX_GENERATED_TEXT_CHARS)]


class ConversationCompleted(StrictModel):
    kind: Literal["completed"] = "completed"
    stamp: GenerationStamp
    finish_reason: FinishReason
    usage: UsageReport
    provider_request_id: ExternalIdentifier | None = None


class ConversationCancelled(StrictModel):
    kind: Literal["cancelled"] = "cancelled"
    stamp: GenerationStamp
    usage: UsageReport


class ConversationFailed(StrictModel):
    kind: Literal["failed"] = "failed"
    stamp: GenerationStamp
    failure: NormalizedFailure
    usage: UsageReport


ConversationEvent = Annotated[
    ConversationStarted
    | ConversationTextDelta
    | ConversationCompleted
    | ConversationCancelled
    | ConversationFailed,
    Field(discriminator="kind"),
]
