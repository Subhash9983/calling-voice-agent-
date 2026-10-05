"""Sarvam failures -> stable normalized TTS failures (docs/09 §14, §24; docs/01 §17).

Only rate limits, provider unavailability, and connect/connection-lost
failures are retryable (and then only before any audio was delivered, which
the orchestrator decides). Authentication, quota, configuration, deadline,
corrupt-audio, and protocol failures are not. Safe messages are fixed strings.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.tts_adapters.sarvam.connection import SarvamErrorKind
from voice_agent.tts_adapters.sarvam.options import SARVAM_PROVIDER

_K = SarvamErrorKind
_BY_KIND: Final[Mapping[SarvamErrorKind, tuple[ErrorType, bool, str]]] = MappingProxyType(
    {
        _K.AUTHENTICATION: (
            ErrorType.AUTHENTICATION_FAILED,
            False,
            "Speech synthesis credentials were rejected.",
        ),
        _K.QUOTA: (ErrorType.QUOTA_EXHAUSTED, False, "The speech synthesis balance is exhausted."),
        _K.RATE_LIMITED: (ErrorType.RATE_LIMITED, True, "Speech synthesis is rate limited."),
        _K.CONFIGURATION: (
            ErrorType.CONFIGURATION_INVALID,
            False,
            "The speech synthesis request was rejected.",
        ),
        _K.UNAVAILABLE: (
            ErrorType.PROVIDER_UNAVAILABLE,
            True,
            "Speech synthesis is temporarily unavailable.",
        ),
        _K.CONNECT_FAILED: (
            ErrorType.CONNECTION_FAILED,
            True,
            "Could not start the speech synthesis stream.",
        ),
        _K.CONNECTION_LOST: (
            ErrorType.CONNECTION_LOST,
            True,
            "The speech synthesis stream was lost.",
        ),
        _K.FIRST_AUDIO_TIMEOUT: (
            ErrorType.PROVIDER_TIMEOUT,
            False,
            "Speech synthesis did not start in time.",
        ),
        _K.TOTAL_TIMEOUT: (
            ErrorType.PROVIDER_TIMEOUT,
            False,
            "Speech synthesis did not finish in time.",
        ),
        _K.CORRUPT_AUDIO: (
            ErrorType.UNKNOWN_PROVIDER_ERROR,
            False,
            "Speech synthesis returned unusable audio.",
        ),
        _K.PROTOCOL: (
            ErrorType.UNKNOWN_PROVIDER_ERROR,
            False,
            "Speech synthesis returned an invalid response.",
        ),
    }
)


def tts_failure(
    kind: SarvamErrorKind,
    *,
    stamp: GenerationStamp,
    occurred_at: datetime,
    phase: str,
    status_code: int | None = None,
) -> NormalizedFailure:
    error_type, retryable, message = _BY_KIND[kind]
    return NormalizedFailure(
        component=ErrorComponent.TTS,
        provider=SARVAM_PROVIDER,
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
