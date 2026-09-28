"""Normalized failures, error taxonomy, and category mapping (docs/01 §17, docs/02 §12).

Raw secrets, headers, provider payloads, and user content never enter a
normalized failure; only a bounded safe message does.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated

from pydantic import Field

from voice_agent.contracts.base import (
    CanonicalId,
    ShortLabel,
    StrictModel,
    UtcDatetime,
)

ERROR_TAXONOMY_VERSION = 1
MAX_SAFE_MESSAGE_LENGTH = 1000


class ErrorType(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    CONFIGURATION_INVALID = "configuration_invalid"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_LOST = "connection_lost"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_AUDIO = "invalid_audio"
    EMPTY_TRANSCRIPT = "empty_transcript"
    CONTENT_REJECTED = "content_rejected"
    CANCELLATION = "cancellation"
    REALTIME_OVERLOAD = "realtime_overload"
    PERSISTENCE_FAILED = "persistence_failed"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"
    INTERNAL_ERROR = "internal_error"


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFIGURATION = "configuration"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    CAPACITY = "capacity"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PROVIDER = "provider"
    INPUT_VALIDATION = "input_validation"
    CONTENT_SAFETY = "content_safety"
    CANCELLATION = "cancellation"
    PERSISTENCE = "persistence"
    INTERNAL = "internal"


class ErrorComponent(StrEnum):
    BROWSER = "browser"
    CONTROL_API = "control_api"
    ORCHESTRATOR = "orchestrator"
    TRANSPORT = "transport"
    STT = "stt"
    CONVERSATION_ENGINE = "conversation_engine"
    TTS = "tts"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    DATABASE = "database"
    COST_ENGINE = "cost_engine"


# Each internal error type maps to exactly one category (docs/01 §17).
# Only ``realtime_overload -> capacity`` is stated explicitly (Decision 067);
# the rest follow the category names in docs/02 §12.
ERROR_CATEGORY_BY_TYPE: Mapping[ErrorType, ErrorCategory] = MappingProxyType(
    {
        ErrorType.AUTHENTICATION_FAILED: ErrorCategory.AUTHENTICATION,
        ErrorType.PERMISSION_DENIED: ErrorCategory.AUTHORIZATION,
        ErrorType.CONFIGURATION_INVALID: ErrorCategory.CONFIGURATION,
        ErrorType.RATE_LIMITED: ErrorCategory.RATE_LIMIT,
        ErrorType.QUOTA_EXHAUSTED: ErrorCategory.QUOTA,
        ErrorType.CONNECTION_FAILED: ErrorCategory.NETWORK,
        ErrorType.CONNECTION_LOST: ErrorCategory.NETWORK,
        ErrorType.PROVIDER_TIMEOUT: ErrorCategory.TIMEOUT,
        ErrorType.PROVIDER_UNAVAILABLE: ErrorCategory.PROVIDER,
        ErrorType.INVALID_AUDIO: ErrorCategory.INPUT_VALIDATION,
        ErrorType.EMPTY_TRANSCRIPT: ErrorCategory.INPUT_VALIDATION,
        ErrorType.CONTENT_REJECTED: ErrorCategory.CONTENT_SAFETY,
        ErrorType.CANCELLATION: ErrorCategory.CANCELLATION,
        ErrorType.REALTIME_OVERLOAD: ErrorCategory.CAPACITY,
        ErrorType.PERSISTENCE_FAILED: ErrorCategory.PERSISTENCE,
        ErrorType.UNKNOWN_PROVIDER_ERROR: ErrorCategory.PROVIDER,
        ErrorType.INTERNAL_ERROR: ErrorCategory.INTERNAL,
    }
)


class NormalizedFailure(StrictModel):
    """Safe, provider-neutral failure record (docs/01 §17)."""

    component: ErrorComponent
    provider: ShortLabel | None = None
    error_type: ErrorType
    safe_message: Annotated[str, Field(min_length=1, max_length=MAX_SAFE_MESSAGE_LENGTH)]
    retryable: bool
    status_code: Annotated[int, Field(ge=0, le=65535)] | None = None
    failure_phase: ShortLabel | None = None
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    occurred_at: UtcDatetime
    user_affected: bool = False
    fallback_succeeded: bool | None = None

    @property
    def category(self) -> ErrorCategory:
        return ERROR_CATEGORY_BY_TYPE[self.error_type]


class NormalizedFailureError(Exception):
    """Exception carrying a :class:`NormalizedFailure` across port boundaries."""

    def __init__(self, failure: NormalizedFailure) -> None:
        super().__init__(f"{failure.component.value}:{failure.error_type.value}")
        self.failure = failure
