"""Conversation history passed to the engine stays within the request contract bound."""

from __future__ import annotations

from voice_agent.contracts.conversation import MAX_HISTORY_MESSAGES, HistoryMessage, HistoryRole
from voice_agent.orchestration.turn_flow import bounded_history

TURN = "00000000-0000-4000-8000-000000000002"


def _message(index: int) -> HistoryMessage:
    role = HistoryRole.USER if index % 2 == 0 else HistoryRole.ASSISTANT
    return HistoryMessage(role=role, text=f"message {index}", turn_id=TURN)


def test_short_history_is_unchanged() -> None:
    history = [_message(i) for i in range(4)]

    assert bounded_history(history) == tuple(history)


def test_long_history_keeps_newest_and_starts_with_a_user_message() -> None:
    history = [_message(i) for i in range(MAX_HISTORY_MESSAGES + 5)]

    bounded = bounded_history(history)

    assert len(bounded) <= MAX_HISTORY_MESSAGES
    assert bounded[0].role is HistoryRole.USER
    assert bounded[-1] == history[-1]
