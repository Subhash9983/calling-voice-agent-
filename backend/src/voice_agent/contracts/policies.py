"""Versioned-configuration policy defaults used by the worker (docs/02 §24, docs/05 §9, §17).

Values here are the approved R&D defaults. Loading them from the immutable
agent configuration is WP3; these models only validate the approved bounds.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_serializer, model_validator

from voice_agent.contracts.base import Probability, StrictModel
from voice_agent.contracts.failures import ErrorType

MAX_RETRYABLE_ERROR_TYPES = 20
MIN_ENDPOINT_DEADLINE_MS = 700
MAX_ENDPOINT_DEADLINE_MS = 1000

# Only allowlisted transient errors retry (docs/05 §17, docs/07 §12, docs/08 §14).
DEFAULT_RETRYABLE_ERROR_TYPES: frozenset[ErrorType] = frozenset(
    {
        ErrorType.RATE_LIMITED,
        ErrorType.CONNECTION_FAILED,
        ErrorType.CONNECTION_LOST,
        ErrorType.PROVIDER_UNAVAILABLE,
    }
)


class RetryPolicy(StrictModel):
    maximum_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    initial_backoff_ms: Annotated[int, Field(ge=0)] = 250
    maximum_backoff_ms: Annotated[int, Field(ge=0)] = 2000
    retryable_error_types: Annotated[
        frozenset[ErrorType], Field(max_length=MAX_RETRYABLE_ERROR_TYPES)
    ] = DEFAULT_RETRYABLE_ERROR_TYPES

    @field_serializer("retryable_error_types")
    def _canonical_order(self, value: frozenset[ErrorType]) -> list[ErrorType]:
        """Sorted, so configuration checksums are identical across processes/hash seeds."""
        return sorted(value)

    @model_validator(mode="after")
    def _ordered_backoff(self) -> RetryPolicy:
        if self.initial_backoff_ms > self.maximum_backoff_ms:
            raise ValueError("initial backoff cannot exceed maximum backoff")
        if ErrorType.CANCELLATION in self.retryable_error_types:
            raise ValueError("cancellation is never retryable")
        return self


class TurnHandlingPolicy(StrictModel):
    """Voice turn-handling defaults (docs/02 §24, docs/03 §8)."""

    activation_threshold: Probability = 0.5
    playback_activation_threshold: Probability = 0.7
    minimum_speech_ms: Annotated[int, Field(ge=0)] = 50
    silence_detection_ms: Annotated[int, Field(gt=0)] = 550
    minimum_interruption_ms: Annotated[int, Field(ge=250)] = 250
    endpoint_deadline_ms: Annotated[
        int, Field(ge=MIN_ENDPOINT_DEADLINE_MS, le=MAX_ENDPOINT_DEADLINE_MS)
    ] = MIN_ENDPOINT_DEADLINE_MS
    stt_finalize_timeout_ms: Annotated[int, Field(gt=0)] = 3000
    interruptions_enabled: bool = True
    false_interruption_suppression: bool = True
    preemptive_generation: bool = False

    @model_validator(mode="after")
    def _approved_combination(self) -> TurnHandlingPolicy:
        if self.preemptive_generation:
            raise ValueError("preemptive generation is disabled in Phase 0")
        if self.playback_activation_threshold < self.activation_threshold:
            raise ValueError("playback activation threshold cannot be below the base threshold")
        return self


class QueueLimits(StrictModel):
    """Initial bounded-queue caps (docs/05 §9); time caps are authoritative."""

    inbound_audio_ms: Annotated[int, Field(gt=0)] = 2000
    response_segments: Annotated[int, Field(gt=0)] = 5
    persistence_events: Annotated[int, Field(gt=0)] = 500
    playback_audio_ms: Annotated[int, Field(gt=0)] = 10_000
