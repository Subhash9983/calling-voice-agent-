"""Instruction-reproduction and credential-shape guard (docs/10 §2 SAFETY)."""

from __future__ import annotations

import pytest

from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION
from voice_agent.response_segmentation.disclosure import DisclosureGuard, DisclosureReason

GUARD = DisclosureGuard.for_instruction(PHASE0_SYSTEM_INSTRUCTION)


@pytest.mark.parametrize(
    "text",
    [
        "Sure: never claim that you searched, verified, booked, called, emailed, updated.",
        "My rules say: RESPOND IN HINDI WHEN THE USER SPEAKS PRIMARILY HINDI!",
        "Main ek friendly general-purpose AI voice assistant running in an R&D environment hoon.",
    ],
)
def test_reproducing_the_instruction_is_disclosure(text: str) -> None:
    assert GUARD.check(text) is DisclosureReason.INSTRUCTION_REPRODUCED


@pytest.mark.parametrize(
    "text",
    [
        "The key is sk-proj-AbCdEf1234567890.",
        "Use Bearer abcdefghijklmnop123 in the header.",
        "Connect with mongodb+srv://cluster.example",
        "Set OPENAI_API_KEY first.",
        "Mera api_key = 12345 hai",
        "It is read from VOICE_AGENT_SECRETS_FILE.",
    ],
)
def test_credential_shapes_are_never_delivered(text: str) -> None:
    assert GUARD.check(text) is DisclosureReason.CREDENTIAL_PATTERN


@pytest.mark.parametrize(
    "text",
    [
        "मैं आपकी मदद कर सकता हूँ।",
        "Mujhe live weather information nahi milti, lekin general tips de sakta hoon.",
        "I don't have access to the product knowledge base yet.",
        "Your OTP is 4 8 2 9 1 7.",
    ],
)
def test_normal_answers_pass(text: str) -> None:
    assert GUARD.check(text) is None


def test_an_empty_instruction_only_checks_credentials() -> None:
    guard = DisclosureGuard.for_instruction("")

    assert guard.check("one two three four five six seven eight nine") is None
    assert guard.check("sk-abcdefghijk") is DisclosureReason.CREDENTIAL_PATTERN
