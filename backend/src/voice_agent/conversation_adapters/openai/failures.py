"""OpenAI failures -> stable normalized conversation failures (docs/08 §21, docs/01 §17).

Only rate limits, temporary connection failures, and provider unavailability
are retryable (docs/08 §14). Authentication, configuration, context size,
safety, tool activity, protocol, and deadline failures are not. Safe
messages are fixed strings; only the numeric status and the failure phase
are kept (the provider request ID travels as operation evidence).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.conversation_adapters.openai.connection import OpenAiErrorKind
from voice_agent.conversation_adapters.openai.options import OPENAI_PROVIDER

_K = OpenAiErrorKind
_BY_KIND: Final[Mapping[OpenAiErrorKind, tuple[ErrorType, bool, str]]] = MappingProxyType(
    {
        _K.AUTHENTICATION: (
            ErrorType.AUTHENTICATION_FAILED,
            False,
            "Conversation credentials were rejected.",
        ),
        _K.PERMISSION: (
            ErrorType.PERMISSION_DENIED,
            False,
            "The conversation model is not permitted for this project.",
        ),
        _K.CONFIGURATION: (
            ErrorType.CONFIGURATION_INVALID,
            False,
            "The conversation request configuration was rejected.",
        ),
        _K.CONTEXT_TOO_LARGE: (
            ErrorType.CONFIGURATION_INVALID,
            False,
            "The conversation context is too large.",
        ),
        _K.RATE_LIMITED: (ErrorType.RATE_LIMITED, True, "The conversation model is rate limited."),
        _K.QUOTA: (
            ErrorType.QUOTA_EXHAUSTED,
            False,
            "The conversation quota or balance is exhausted.",
        ),
        _K.UNAVAILABLE: (
            ErrorType.PROVIDER_UNAVAILABLE,
            True,
            "The conversation model is temporarily unavailable.",
        ),
        _K.CONNECT_FAILED: (
            ErrorType.CONNECTION_FAILED,
            True,
            "Could not start the conversation stream.",
        ),
        _K.CONNECTION_LOST: (
            ErrorType.CONNECTION_LOST,
            True,
            "The conversation stream was lost.",
        ),
        _K.FIRST_TOKEN_TIMEOUT: (
            ErrorType.PROVIDER_TIMEOUT,
            False,
            "The conversation model did not start responding in time.",
        ),
        _K.TOTAL_TIMEOUT: (
            ErrorType.PROVIDER_TIMEOUT,
            False,
            "The conversation response did not finish in time.",
        ),
        _K.SAFETY: (
            ErrorType.CONTENT_REJECTED,
            False,
            "The conversation provider rejected the content.",
        ),
        _K.TOOL_ACTIVITY: (
            ErrorType.CONFIGURATION_INVALID,
            False,
            "The conversation stream reported tool activity, which is disabled.",
        ),
        _K.PROTOCOL: (
            ErrorType.UNKNOWN_PROVIDER_ERROR,
            False,
            "The conversation stream returned an invalid response.",
        ),
    }
)


def conversation_failure(
    kind: OpenAiErrorKind,
    *,
    stamp: GenerationStamp,
    occurred_at: datetime,
    phase: str,
    status_code: int | None = None,
) -> NormalizedFailure:
    error_type, retryable, message = _BY_KIND[kind]
    return NormalizedFailure(
        component=ErrorComponent.CONVERSATION_ENGINE,
        provider=OPENAI_PROVIDER,
        error_type=error_type,
        safe_message=message,
        retryable=retryable,
        status_code=status_code,
        failure_phase=phase,
        session_id=stamp.session_id,
        turn_id=stamp.turn_id,
        operation_id=stamp.operation_id,
        occurred_at=occurred_at,
        user_affected=True,
    )
