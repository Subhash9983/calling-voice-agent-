"""Conservative pre-request token estimate (docs/08 §8: counts may be safely estimated)."""

from __future__ import annotations

from voice_agent.costing.token_estimate import (
    MESSAGE_OVERHEAD_TOKENS,
    estimate_message_tokens,
    estimate_text_tokens,
)


def test_empty_text_is_zero_and_a_message_has_fixed_overhead() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_message_tokens("") == MESSAGE_OVERHEAD_TOKENS


def test_estimate_over_counts_english_and_devanagari() -> None:
    english = "Please tell me the weather tomorrow in Mumbai."
    hindi = "कृपया मुझे कल मुंबई का मौसम बताइए।"

    # Real tokenizers use roughly 4 bytes per English token and 3-6 bytes per
    # Devanagari token; two bytes per token keeps the estimate on the safe side.
    assert estimate_text_tokens(english) >= len(english) // 4
    assert estimate_text_tokens(hindi) >= len(hindi)
    assert estimate_text_tokens("a") == 1


def test_estimate_is_monotonic_in_length() -> None:
    assert estimate_text_tokens("नमस्ते " * 20) > estimate_text_tokens("नमस्ते " * 10)
