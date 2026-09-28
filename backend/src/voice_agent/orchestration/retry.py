"""Retry classification and bounded exponential backoff (docs/01 §16, docs/05 §17).

- retry only allowlisted transient errors that the adapter marked retryable;
- at most ``maximum_attempts`` total attempts per logical request;
- cancellation or deadline stops retries immediately;
- once output was delivered/spoken, never restart the full response;
- non-idempotent actions never retry automatically.

Jitter is injected (a unit fraction) so tests stay deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from voice_agent.contracts.failures import ErrorType, NormalizedFailure
from voice_agent.contracts.policies import RetryPolicy

_HALF_UP = 0.5


class RetryBlockedReason(StrEnum):
    """docs/02 §12 ``retry_blocked_reason`` values."""

    NOT_RETRYABLE = "not_retryable"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    SESSION_CANCELLED = "session_cancelled"
    TURN_CANCELLED = "turn_cancelled"
    NON_IDEMPOTENT_ACTION = "non_idempotent_action"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True, slots=True)
class RetryContext:
    attempt_number: int
    session_cancelled: bool = False
    turn_cancelled: bool = False
    deadline_exceeded: bool = False
    output_delivered: bool = False
    idempotent: bool = True


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    blocked_reason: RetryBlockedReason | None = None
    backoff_ms: int | None = None


def is_retryable(policy: RetryPolicy, failure: NormalizedFailure) -> bool:
    return (
        failure.retryable
        and failure.error_type is not ErrorType.CANCELLATION
        and failure.error_type in policy.retryable_error_types
    )


def _blocked_reason(
    policy: RetryPolicy, failure: NormalizedFailure, context: RetryContext
) -> RetryBlockedReason | None:
    if context.session_cancelled:
        return RetryBlockedReason.SESSION_CANCELLED
    if context.turn_cancelled:
        return RetryBlockedReason.TURN_CANCELLED
    if context.deadline_exceeded:
        return RetryBlockedReason.DEADLINE_EXCEEDED
    if not context.idempotent:
        return RetryBlockedReason.NON_IDEMPOTENT_ACTION
    # A delivered partial response is never restarted (docs/05 §14); docs/02
    # has no dedicated reason, so it is classified as not retryable.
    if context.output_delivered or not is_retryable(policy, failure):
        return RetryBlockedReason.NOT_RETRYABLE
    if context.attempt_number >= policy.maximum_attempts:
        return RetryBlockedReason.ATTEMPT_LIMIT_REACHED
    return None


def backoff_ms(policy: RetryPolicy, failed_attempt: int, jitter: float) -> int:
    """Exponential backoff with equal jitter: ``base/2 + jitter * base/2``, capped."""
    if not 0.0 <= jitter <= 1.0:
        raise ValueError("jitter must be within [0, 1]")
    exponent = max(failed_attempt - 1, 0)
    base = min(policy.maximum_backoff_ms, policy.initial_backoff_ms * (1 << exponent))
    half = base / 2
    return math.floor(half + jitter * half + _HALF_UP)


def decide_retry(
    policy: RetryPolicy,
    failure: NormalizedFailure,
    context: RetryContext,
    *,
    jitter: float,
) -> RetryDecision:
    reason = _blocked_reason(policy, failure, context)
    if reason is not None:
        return RetryDecision(should_retry=False, blocked_reason=reason)
    return RetryDecision(
        should_retry=True, backoff_ms=backoff_ms(policy, context.attempt_number, jitter)
    )
