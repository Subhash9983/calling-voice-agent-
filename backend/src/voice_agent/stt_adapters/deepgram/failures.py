"""Deepgram failures -> stable normalized STT failures (docs/07 §20, docs/01 §17).

Only the allowlisted transient kinds are retryable. Safe messages are fixed
strings; no provider body, header, URL, transcript, or credential is kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.stt_adapters.deepgram.connection import DeepgramErrorKind
from voice_agent.stt_adapters.deepgram.options import DEEPGRAM_PROVIDER

_BY_KIND: Final[Mapping[DeepgramErrorKind, tuple[ErrorType, bool, str]]] = MappingProxyType(
    {
        DeepgramErrorKind.AUTHENTICATION: (
            ErrorType.AUTHENTICATION_FAILED,
            False,
            "Speech recognition credentials were rejected.",
        ),
        DeepgramErrorKind.CONFIGURATION: (
            ErrorType.CONFIGURATION_INVALID,
            False,
            "Speech recognition configuration was rejected.",
        ),
        DeepgramErrorKind.QUOTA: (
            ErrorType.QUOTA_EXHAUSTED,
            False,
            "Speech recognition quota or balance is exhausted.",
        ),
        DeepgramErrorKind.RATE_LIMITED: (
            ErrorType.RATE_LIMITED,
            True,
            "Speech recognition is rate limited.",
        ),
        DeepgramErrorKind.UNAVAILABLE: (
            ErrorType.PROVIDER_UNAVAILABLE,
            True,
            "Speech recognition is temporarily unavailable.",
        ),
        DeepgramErrorKind.CONNECT_FAILED: (
            ErrorType.CONNECTION_FAILED,
            True,
            "Could not connect to speech recognition.",
        ),
        DeepgramErrorKind.CONNECTION_LOST: (
            ErrorType.CONNECTION_LOST,
            True,
            "The speech recognition stream was lost.",
        ),
        DeepgramErrorKind.TIMEOUT: (
            ErrorType.PROVIDER_TIMEOUT,
            False,
            "Speech recognition did not respond in time.",
        ),
        DeepgramErrorKind.PROTOCOL: (
            ErrorType.UNKNOWN_PROVIDER_ERROR,
            False,
            "Speech recognition returned an invalid response.",
        ),
    }
)


def stt_failure(
    kind: DeepgramErrorKind,
    *,
    session_id: str,
    occurred_at: datetime,
    phase: str,
    status_code: int | None = None,
    turn_id: str | None = None,
    operation_id: str | None = None,
    user_affected: bool = False,
) -> NormalizedFailure:
    error_type, retryable, message = _BY_KIND[kind]
    return NormalizedFailure(
        component=ErrorComponent.STT,
        provider=DEEPGRAM_PROVIDER,
        error_type=error_type,
        safe_message=message,
        retryable=retryable,
        status_code=status_code,
        failure_phase=phase,
        session_id=session_id,
        turn_id=turn_id,
        operation_id=operation_id,
        occurred_at=occurred_at,
        user_affected=user_affected,
    )
