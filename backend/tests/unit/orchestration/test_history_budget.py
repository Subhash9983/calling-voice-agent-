"""Token-budgeted history: oldest complete turns go first, never mid-message (docs/08 §8)."""

from __future__ import annotations

from voice_agent.contracts.conversation import HistoryMessage, HistoryRole
from voice_agent.costing.token_estimate import estimate_message_tokens
from voice_agent.orchestration.history_budget import budget_history


def _turn(index: int, user: str, assistant: str | None) -> list[HistoryMessage]:
    turn_id = f"00000000-0000-4000-8000-{index:012d}"
    messages = [HistoryMessage(role=HistoryRole.USER, text=user, turn_id=turn_id)]
    if assistant is not None:
        messages.append(HistoryMessage(role=HistoryRole.ASSISTANT, text=assistant, turn_id=turn_id))
    return messages


def test_small_history_is_kept_whole() -> None:
    history = [*_turn(1, "Namaste", "Namaste! Kaise madad karun?"), *_turn(2, "Theek", None)]

    budget = budget_history(history, system_instruction="S", user_transcript="Aage?")

    assert budget.messages == tuple(history)
    assert not budget.truncated
    assert budget.dropped_turns == 0
    assert budget.estimated_input_tokens == (
        estimate_message_tokens("S")
        + estimate_message_tokens("Aage?")
        + budget.estimated_history_tokens
    )


def test_oldest_complete_turns_are_dropped_first() -> None:
    long_text = "शब्द " * 200  # about 1,000 bytes per message
    history = [
        message
        for index in range(1, 7)
        for message in _turn(index, f"{index} {long_text}", f"{index} {long_text}")
    ]
    per_turn = 2 * estimate_message_tokens(f"1 {long_text}")

    budget = budget_history(
        history,
        system_instruction="S",
        user_transcript="now",
        history_budget=per_turn * 2 + 1,
    )

    assert budget.dropped_turns == 4
    assert budget.dropped_messages == 8
    assert [m.text[:1] for m in budget.messages] == ["5", "5", "6", "6"]
    assert budget.truncated


def test_total_input_budget_shrinks_history_and_never_cuts_a_message() -> None:
    history = [*_turn(1, "a" * 400, "b" * 400), *_turn(2, "c" * 40, "d" * 40)]

    budget = budget_history(
        history,
        system_instruction="x" * 1000,
        user_transcript="y",
        total_budget=estimate_message_tokens("x" * 1000) + estimate_message_tokens("y") + 60,
    )

    assert [m.text for m in budget.messages] == ["c" * 40, "d" * 40]


def test_history_without_room_is_empty_and_starts_on_a_user_message() -> None:
    orphan = HistoryMessage(
        role=HistoryRole.ASSISTANT, text="dangling", turn_id="00000000-0000-4000-8000-000000000009"
    )
    history = [orphan, *_turn(1, "hi", "hello")]

    tight = budget_history(history, system_instruction="S", user_transcript="u", history_budget=0)
    roomy = budget_history(history, system_instruction="S", user_transcript="u")

    assert tight.messages == ()
    assert tight.dropped_turns == 2
    assert roomy.messages[0].role is HistoryRole.USER
    assert roomy.dropped_messages == 1
