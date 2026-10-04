"""Bounded conversation history for one request (docs/08 §7-§8, docs/05 §13).

Operating targets: system instruction about 2,000 tokens, history at most
12,000 tokens, complete request input at most 16,000 tokens. When history
exceeds the budget the system instruction and the current accepted turn are
always kept, the newest complete turns that fit are kept, and the oldest
complete turns (all messages sharing a ``turn_id``) are removed first. A
message is never cut in the middle and no summarization model runs. Token
counts are the conservative estimate from :mod:`voice_agent.costing.token_estimate`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from voice_agent.contracts.conversation import MAX_HISTORY_MESSAGES, HistoryMessage, HistoryRole
from voice_agent.costing.token_estimate import estimate_message_tokens

HISTORY_TOKEN_BUDGET: Final = 12_000
TOTAL_INPUT_TOKEN_BUDGET: Final = 16_000


@dataclass(frozen=True, slots=True)
class HistoryBudget:
    """The bounded history plus safe truncation evidence (counts only)."""

    messages: tuple[HistoryMessage, ...]
    dropped_turns: int
    dropped_messages: int
    estimated_history_tokens: int
    estimated_input_tokens: int

    @property
    def truncated(self) -> bool:
        return self.dropped_messages > 0


def _turn_groups(history: Sequence[HistoryMessage]) -> list[list[HistoryMessage]]:
    groups: list[list[HistoryMessage]] = []
    for message in history:
        if groups and groups[-1][0].turn_id == message.turn_id:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _group_tokens(group: Sequence[HistoryMessage]) -> int:
    return sum(estimate_message_tokens(message.text) for message in group)


def _starts_with_user(groups: list[list[HistoryMessage]]) -> list[list[HistoryMessage]]:
    while groups and groups[0][0].role is not HistoryRole.USER:
        groups = groups[1:]
    return groups


def budget_history(
    history: Sequence[HistoryMessage],
    *,
    system_instruction: str,
    user_transcript: str,
    history_budget: int = HISTORY_TOKEN_BUDGET,
    total_budget: int = TOTAL_INPUT_TOKEN_BUDGET,
) -> HistoryBudget:
    fixed = estimate_message_tokens(system_instruction) + estimate_message_tokens(user_transcript)
    available = max(min(history_budget, total_budget - fixed), 0)
    kept: list[list[HistoryMessage]] = []
    used = 0
    count = 0
    for group in reversed(_turn_groups(history)):
        cost = _group_tokens(group)
        if used + cost > available or count + len(group) > MAX_HISTORY_MESSAGES:
            break
        kept.insert(0, group)
        used += cost
        count += len(group)
    kept = _starts_with_user(kept)
    messages = tuple(message for group in kept for message in group)
    used = sum(_group_tokens(group) for group in kept)
    all_groups = _turn_groups(history)
    return HistoryBudget(
        messages=messages,
        dropped_turns=len(all_groups) - len(kept),
        dropped_messages=len(history) - len(messages),
        estimated_history_tokens=used,
        estimated_input_tokens=fixed + used,
    )
