"""Retry classification and bounded backoff (docs/01 §16, docs/05 §17, docs/02 §24)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.orchestration.retry import (
    RetryBlockedReason,
    RetryContext,
    backoff_ms,
    decide_retry,
)

SESSION = str(uuid.UUID(int=1, version=4))
POLICY = RetryPolicy()


def _failure(error_type: ErrorType, *, retryable: bool = True) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.CONVERSATION_ENGINE,
        provider="mock",
        error_type=error_type,
        safe_message="safe",
        retryable=retryable,
        session_id=SESSION,
        occurred_at=datetime(2026, 9, 28, tzinfo=UTC),
    )


def _context(**overrides: object) -> RetryContext:
    values: dict[str, object] = {"attempt_number": 1}
    values.update(overrides)
    return RetryContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.RATE_LIMITED,
        ErrorType.CONNECTION_FAILED,
        ErrorType.CONNECTION_LOST,
        ErrorType.PROVIDER_UNAVAILABLE,
    ],
)
def test_allowlisted_transient_errors_retry(error_type: ErrorType) -> None:
    decision = decide_retry(POLICY, _failure(error_type), _context(), jitter=1.0)

    assert decision.should_retry
    assert decision.blocked_reason is None
    assert decision.backoff_ms == POLICY.initial_backoff_ms


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.AUTHENTICATION_FAILED,
        ErrorType.PERMISSION_DENIED,
        ErrorType.CONFIGURATION_INVALID,
        ErrorType.QUOTA_EXHAUSTED,
        ErrorType.PROVIDER_TIMEOUT,
        ErrorType.CONTENT_REJECTED,
        ErrorType.CANCELLATION,
        ErrorType.EMPTY_TRANSCRIPT,
        ErrorType.REALTIME_OVERLOAD,
        ErrorType.INTERNAL_ERROR,
    ],
)
def test_non_allowlisted_errors_never_retry(error_type: ErrorType) -> None:
    decision = decide_retry(POLICY, _failure(error_type), _context(), jitter=0.0)

    assert not decision.should_retry
    assert decision.blocked_reason is RetryBlockedReason.NOT_RETRYABLE


def test_adapter_marked_non_retryable_wins_over_allowlist() -> None:
    failure = _failure(ErrorType.RATE_LIMITED, retryable=False)

    decision = decide_retry(POLICY, failure, _context(), jitter=0.0)

    assert decision.blocked_reason is RetryBlockedReason.NOT_RETRYABLE


def test_attempt_limit_is_three_total_attempts() -> None:
    failure = _failure(ErrorType.RATE_LIMITED)

    assert decide_retry(POLICY, failure, _context(attempt_number=2), jitter=0.0).should_retry
    third = decide_retry(POLICY, failure, _context(attempt_number=3), jitter=0.0)

    assert third.blocked_reason is RetryBlockedReason.ATTEMPT_LIMIT_REACHED


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"session_cancelled": True}, RetryBlockedReason.SESSION_CANCELLED),
        ({"turn_cancelled": True}, RetryBlockedReason.TURN_CANCELLED),
        ({"deadline_exceeded": True}, RetryBlockedReason.DEADLINE_EXCEEDED),
        ({"idempotent": False}, RetryBlockedReason.NON_IDEMPOTENT_ACTION),
        ({"output_delivered": True}, RetryBlockedReason.NOT_RETRYABLE),
    ],
)
def test_cancellation_deadline_delivery_and_idempotency_stop_retries(
    overrides: dict[str, object], reason: RetryBlockedReason
) -> None:
    decision = decide_retry(
        POLICY, _failure(ErrorType.RATE_LIMITED), _context(**overrides), jitter=0.0
    )

    assert not decision.should_retry
    assert decision.blocked_reason is reason


@pytest.mark.parametrize(
    ("attempt", "jitter", "expected"),
    [
        (1, 1.0, 250),
        (1, 0.0, 125),
        (2, 1.0, 500),
        (3, 1.0, 1000),
        (4, 1.0, 2000),
        (9, 1.0, 2000),
        (9, 0.0, 1000),
    ],
)
def test_exponential_backoff_with_equal_jitter_is_capped(
    attempt: int, jitter: float, expected: int
) -> None:
    assert backoff_ms(POLICY, attempt, jitter) == expected


def test_jitter_must_be_a_unit_fraction() -> None:
    with pytest.raises(ValueError, match="jitter"):
        backoff_ms(POLICY, 1, 1.5)


def test_policy_rejects_cancellation_as_retryable() -> None:
    with pytest.raises(ValueError, match="cancellation"):
        RetryPolicy(retryable_error_types=frozenset({ErrorType.CANCELLATION}))
