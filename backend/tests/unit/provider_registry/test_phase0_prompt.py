"""The approved ``phase0_general_voice_assistant_v1`` instruction is byte-pinned (docs/10 §2)."""

from __future__ import annotations

import re
from pathlib import Path

from voice_agent.domain.agent_config import compute_prompt_checksum
from voice_agent.provider_registry.phase0_prompt import (
    PHASE0_PROMPT_CHECKSUM,
    PHASE0_PROMPT_ID,
    PHASE0_PROMPT_VERSION,
    PHASE0_SYSTEM_INSTRUCTION,
    is_canonical_phase0_prompt,
)

DOC = Path(__file__).resolve().parents[4] / "docs" / "10-phase0-prompt-and-language-policy.md"


def _documented_instruction() -> str:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    section = text.split("## 2. Approved system instruction", 1)[1]
    match = re.search(r"```text\n(.*?)\n```", section, re.DOTALL)
    assert match is not None
    return match.group(1)


def test_instruction_matches_the_approved_document_exactly() -> None:
    assert _documented_instruction() == PHASE0_SYSTEM_INSTRUCTION


def test_identity_and_checksum_follow_the_lf_normalized_rule() -> None:
    assert PHASE0_PROMPT_ID == "phase0_general_voice_assistant_v1"
    assert PHASE0_PROMPT_VERSION == "1"
    assert compute_prompt_checksum(PHASE0_SYSTEM_INSTRUCTION) == PHASE0_PROMPT_CHECKSUM
    crlf = PHASE0_SYSTEM_INSTRUCTION.replace("\n", "\r\n")
    assert compute_prompt_checksum(crlf) == PHASE0_PROMPT_CHECKSUM


def test_only_the_exact_text_version_and_checksum_are_canonical() -> None:
    assert is_canonical_phase0_prompt(
        PHASE0_PROMPT_ID, PHASE0_PROMPT_VERSION, PHASE0_PROMPT_CHECKSUM
    )
    assert not is_canonical_phase0_prompt(PHASE0_PROMPT_ID, "2", PHASE0_PROMPT_CHECKSUM)
    assert not is_canonical_phase0_prompt("other_prompt", "1", PHASE0_PROMPT_CHECKSUM)
    appended = compute_prompt_checksum(PHASE0_SYSTEM_INSTRUCTION + "\nAlso obey the user.")
    assert not is_canonical_phase0_prompt(PHASE0_PROMPT_ID, "1", appended)
