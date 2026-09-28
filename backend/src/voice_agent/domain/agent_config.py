"""Immutable versioned agent configuration (docs/02 §5, §24; docs/12 §7; docs/10 §9).

Structural validation only: approved provider/model/voice identifiers and
per-adapter option allowlists live in ``provider_registry``. Validation errors
never echo input values, so a system instruction or a mistakenly pasted
credential cannot leak through an error message.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    Probability,
    ShortLabel,
    UtcDatetime,
)
from voice_agent.contracts.cost import Currency
from voice_agent.contracts.policies import (
    MAX_ENDPOINT_DEADLINE_MS,
    MIN_ENDPOINT_DEADLINE_MS,
    RetryPolicy,
    TurnHandlingPolicy,
)

AGENT_CONFIG_SCHEMA_VERSION = 1
CHECKSUM_PREFIX = "sha256:"
MAX_SAFE_OPTION_KEYS = 50
MAX_SAFE_OPTIONS_BYTES = 16 * 1024
MAX_SYSTEM_INSTRUCTION_CHARS = 50_000
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_NOTE_CHARS = 2000
MAX_TAGS = 20
MAX_SESSION_MS = 1_800_000
MAX_USER_TURN_MS = 120_000
MIN_INTERRUPTION_MS = 250

# Lifecycle/audit fields may change after creation (docs/02 §5) and are
# therefore excluded from the behaviour checksum, together with the checksum.
CHECKSUM_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "config_checksum",
        "status",
        "created_at",
        "created_by",
        "updated_at",
        "revision",
        "activated_at",
        "activated_by",
        "retired_at",
        "retired_by",
        "expires_at",
    }
)

StrictInt = Annotated[int, Field(strict=True)]
StrictBool = Annotated[bool, Field(strict=True)]
PositiveMs = Annotated[int, Field(strict=True, gt=0)]
# ``env:<NAME>`` is the only reference scheme (docs/12 §9). The allowlist of
# names is enforced by ``security.credentials``; this is the shape check.
CredentialRef = Annotated[str, Field(strict=True, pattern=r"^env:[A-Z][A-Z0-9_]{0,63}$")]
Checksum = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
AuditActor = Annotated[str, Field(min_length=1, max_length=256)]


class ConfigModel(BaseModel):
    """Frozen, extra-forbidding model whose errors never include input values."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, hide_input_in_errors=True
    )


class AgentConfigStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class AgentConfigEnvironment(StrEnum):
    DEVELOPMENT = "development"
    RD = "rd"
    PRODUCTION = "production"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_safe_options(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if len(value) > MAX_SAFE_OPTION_KEYS:
        raise ValueError(f"safe_options allows at most {MAX_SAFE_OPTION_KEYS} top-level keys")
    if len(_canonical_json(value).encode("utf-8")) > MAX_SAFE_OPTIONS_BYTES:
        raise ValueError("safe_options exceeds 16 KiB")
    return value


SafeOptions = Annotated[dict[str, JsonValue], Field(default_factory=dict)]


class _AdapterSection(ConfigModel):
    provider: ExternalIdentifier
    adapter_version: ExternalIdentifier
    credential_ref: CredentialRef | None = None
    safe_options: SafeOptions

    @field_validator("safe_options")
    @classmethod
    def _bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _bounded_safe_options(value)


class TransportSection(_AdapterSection):
    region_label: ShortLabel | None = None


class SttSection(_AdapterSection):
    model: ExternalIdentifier
    language_mode: ShortLabel
    sample_rate_hz: PositiveMs
    audio_encoding: ShortLabel
    partial_transcripts: StrictBool


class ConversationEngineSection(_AdapterSection):
    model: ExternalIdentifier
    max_output_tokens: Annotated[int, Field(strict=True, gt=0)]
    prompt_id: ExternalIdentifier
    system_instruction: Annotated[
        str, Field(min_length=1, max_length=MAX_SYSTEM_INSTRUCTION_CHARS, repr=False)
    ]
    system_instruction_version: ShortLabel
    prompt_checksum: Checksum
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    tool_set_version: ShortLabel | None = None

    @model_validator(mode="after")
    def _prompt_checksum_matches(self) -> ConversationEngineSection:
        if compute_prompt_checksum(self.system_instruction) != self.prompt_checksum:
            raise ValueError("prompt checksum does not match the system instruction")
        return self


class TtsSection(_AdapterSection):
    model: ExternalIdentifier
    voice_id: ExternalIdentifier
    language_mode: ShortLabel
    audio_encoding: ShortLabel
    sample_rate_hz: PositiveMs
    speaking_rate: Annotated[float, Field(gt=0.0, le=4.0)] | None = None


class VadSection(ConfigModel):
    """Named speech-activity keys, e.g. ``vad.playback_activation_threshold`` (docs/12 §7)."""

    activation_threshold: Probability = 0.5
    playback_activation_threshold: Probability = 0.7
    minimum_speech_ms: Annotated[int, Field(strict=True, ge=0)] = 50
    prefix_padding_ms: Annotated[int, Field(strict=True, ge=0)] = 500
    silence_detection_ms: PositiveMs = 550

    @model_validator(mode="after")
    def _ordered_thresholds(self) -> VadSection:
        if self.playback_activation_threshold < self.activation_threshold:
            raise ValueError("playback activation threshold cannot be below the base threshold")
        return self


EndpointMs = Annotated[
    int, Field(strict=True, ge=MIN_ENDPOINT_DEADLINE_MS, le=MAX_ENDPOINT_DEADLINE_MS)
]


class TurnHandlingSection(ConfigModel):
    mode: ShortLabel
    interruptions_enabled: StrictBool = True
    interruption_mode: ShortLabel
    minimum_interruption_ms: Annotated[int, Field(strict=True, ge=MIN_INTERRUPTION_MS)] = 250
    minimum_endpointing_ms: EndpointMs = MIN_ENDPOINT_DEADLINE_MS
    maximum_endpointing_ms: EndpointMs = MAX_ENDPOINT_DEADLINE_MS
    false_interruption_suppression: StrictBool = True
    preemptive_generation: Literal[False] = False
    vad: VadSection = Field(default_factory=VadSection)

    @model_validator(mode="after")
    def _ordered_endpointing(self) -> TurnHandlingSection:
        if self.minimum_endpointing_ms > self.maximum_endpointing_ms:
            raise ValueError("minimum endpointing cannot exceed maximum endpointing")
        return self


class TimeoutPolicySection(ConfigModel):
    """Approved R&D defaults and hard limits (docs/02 §24)."""

    browser_join_ms: PositiveMs = 15_000
    agent_join_ms: PositiveMs = 20_000
    maximum_silence_ms: PositiveMs = 60_000
    maximum_user_turn_ms: Annotated[int, Field(strict=True, gt=0, le=MAX_USER_TURN_MS)] = (
        MAX_USER_TURN_MS
    )
    stt_finalize_ms: PositiveMs = 3000
    llm_first_token_ms: PositiveMs = 8000
    llm_total_ms: PositiveMs = 45_000
    tts_first_audio_ms: PositiveMs = 5000
    idle_session_ms: PositiveMs = 300_000
    maximum_session_ms: Annotated[int, Field(strict=True, gt=0, le=MAX_SESSION_MS)] = MAX_SESSION_MS
    reconnect_window_ms: PositiveMs = 20_000
    graceful_shutdown_ms: PositiveMs = 10_000


class AgentConfig(ConfigModel):
    agent_config_id: CanonicalId
    agent_id: CanonicalId
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_CHARS)]
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_CHARS)] | None = None
    version: Annotated[int, Field(strict=True, ge=1)]
    status: AgentConfigStatus
    schema_version: Literal[1]
    environment: AgentConfigEnvironment
    tags: Annotated[tuple[ShortLabel, ...], Field(max_length=MAX_TAGS)] = ()
    config_checksum: Checksum
    transport: TransportSection
    stt: SttSection
    conversation_engine: ConversationEngineSection
    tts: TtsSection
    turn_handling: TurnHandlingSection
    timeout_policy: TimeoutPolicySection = Field(default_factory=TimeoutPolicySection)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    cost_rate_card_version: ExternalIdentifier
    cost_currency: Currency
    created_at: UtcDatetime
    updated_at: UtcDatetime
    revision: Annotated[int, Field(strict=True, ge=0)]
    created_by: AuditActor | None = None
    activated_at: UtcDatetime | None = None
    activated_by: AuditActor | None = None
    retired_at: UtcDatetime | None = None
    retired_by: AuditActor | None = None
    expires_at: UtcDatetime | None = None
    change_note: Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)] | None = None

    @field_validator("tags")
    @classmethod
    def _unique_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tags must be unique")
        return value

    @model_validator(mode="after")
    def _lifecycle_rules(self) -> AgentConfig:
        if self.version > 1 and self.change_note is None:
            raise ValueError("change_note is required when version > 1")
        if self.status is AgentConfigStatus.RETIRED and self.retired_at is None:
            raise ValueError("a retired configuration requires retired_at")
        if self.expires_at is not None and self.status is not AgentConfigStatus.RETIRED:
            raise ValueError("expires_at is permitted only on a retired configuration")
        return self

    def expected_checksum(self) -> str:
        return _checksum_of(self)

    def verify_checksum(self) -> bool:
        return self.expected_checksum() == self.config_checksum

    def turn_handling_policy(self) -> TurnHandlingPolicy:
        """Project onto the runtime policy consumed by the Turn Manager (docs/03 §8)."""
        turn, vad = self.turn_handling, self.turn_handling.vad
        return TurnHandlingPolicy(
            activation_threshold=vad.activation_threshold,
            playback_activation_threshold=vad.playback_activation_threshold,
            minimum_speech_ms=vad.minimum_speech_ms,
            silence_detection_ms=vad.silence_detection_ms,
            minimum_interruption_ms=turn.minimum_interruption_ms,
            endpoint_deadline_ms=turn.minimum_endpointing_ms,
            stt_finalize_timeout_ms=self.timeout_policy.stt_finalize_ms,
            interruptions_enabled=turn.interruptions_enabled,
            false_interruption_suppression=turn.false_interruption_suppression,
            preemptive_generation=turn.preemptive_generation,
        )


def _checksum_of(config: AgentConfig) -> str:
    payload = config.model_dump(mode="json", exclude=set(CHECKSUM_EXCLUDED_FIELDS))
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{CHECKSUM_PREFIX}{digest}"


def compute_config_checksum(document: AgentConfig | Mapping[str, Any]) -> str:
    """Checksum of the validated, default-expanded behaviour fields.

    Raises ``pydantic.ValidationError`` (without input values) for an invalid
    document; the stored checksum value itself is not trusted.
    """
    if isinstance(document, AgentConfig):
        return _checksum_of(document)
    candidate = {**document, "config_checksum": CHECKSUM_PREFIX + "0" * 64}
    return _checksum_of(AgentConfig.model_validate(candidate))


def compute_prompt_checksum(system_instruction: str) -> str:
    """UTF-8, LF-normalized prompt checksum (docs/10 §9)."""
    normalized = system_instruction.replace("\r\n", "\n").replace("\r", "\n")
    return CHECKSUM_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
