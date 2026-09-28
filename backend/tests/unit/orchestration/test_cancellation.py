"""Hierarchical cancellation: session -> turn -> operation (docs/01 §15)."""

from __future__ import annotations

import pytest

from voice_agent.orchestration.cancellation import CancellationScope, CancelledScopeError


def test_session_cancellation_cascades_to_turns_and_operations() -> None:
    session = CancellationScope("session")
    turn = session.child("turn")
    operation = turn.child("operation")

    session.cancel("session_end")

    assert turn.is_cancelled
    assert operation.is_cancelled
    assert operation.reason == "session_end"


def test_operation_cancellation_does_not_cancel_parents_or_siblings() -> None:
    turn = CancellationScope("turn")
    first = turn.child("op-1")
    second = turn.child("op-2")

    first.cancel("retry")

    assert first.is_cancelled
    assert not second.is_cancelled
    assert not turn.is_cancelled


def test_child_of_cancelled_scope_starts_cancelled() -> None:
    turn = CancellationScope("turn")
    turn.cancel("user_interruption")

    late = turn.child("op")

    assert late.is_cancelled
    assert late.reason == "user_interruption"


def test_callbacks_run_once_in_registration_order() -> None:
    scope = CancellationScope("turn")
    calls: list[str] = []
    scope.on_cancel(lambda reason: calls.append(f"a:{reason}"))
    scope.on_cancel(lambda reason: calls.append(f"b:{reason}"))

    scope.cancel("x")
    scope.cancel("y")

    assert calls == ["a:x", "b:x"]
    assert scope.reason == "x"


def test_callback_registered_after_cancel_runs_immediately() -> None:
    scope = CancellationScope("turn")
    scope.cancel("done")
    calls: list[str] = []

    scope.on_cancel(calls.append)

    assert calls == ["done"]


def test_raise_if_cancelled() -> None:
    scope = CancellationScope("op")
    scope.raise_if_cancelled()

    scope.cancel("timeout")

    with pytest.raises(CancelledScopeError, match="timeout"):
        scope.raise_if_cancelled()
