"""Pre-delivery disclosure guard (docs/08 §11, docs/10 §2 SAFETY, §8).

Complements :func:`validate_segment` with two checks that need context the
generic validator lacks:

- *instruction reproduction*: a candidate segment that repeats any run of
  ``SHINGLE_WORDS`` consecutive words of the active system instruction
  (case/punctuation-insensitive) is treated as prompt disclosure;
- *credential shapes*: API-key prefixes, bearer tokens, connection strings,
  and the configured credential variable names are never delivered.

A hit stops delivery of the response; it is never repaired or paraphrased.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

SHINGLE_WORDS: Final = 8
_WORD: Final = re.compile(r"[^\W_]+", re.UNICODE)
_CREDENTIAL: Final = re.compile(
    r"(\bsk-[A-Za-z0-9_-]{8,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/-]{8,}"
    r"|mongodb(?:\+srv)?://"
    r"|\b[A-Z][A-Z0-9]*_(?:API_KEY|API_SECRET|SECRET|TOKEN|PASSWORD)\b"
    r"|\bVOICE_AGENT_SECRETS_FILE\b"
    r"|\bapi[_ ]?key\s*[:=])",
    re.IGNORECASE,
)


class DisclosureReason(StrEnum):
    INSTRUCTION_REPRODUCED = "instruction_reproduced"
    CREDENTIAL_PATTERN = "credential_pattern"


def _words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def _shingles(words: list[str]) -> frozenset[tuple[str, ...]]:
    return frozenset(
        tuple(words[index : index + SHINGLE_WORDS])
        for index in range(len(words) - SHINGLE_WORDS + 1)
    )


@dataclass(frozen=True, slots=True)
class DisclosureGuard:
    """Built once per configuration from the exact active instruction."""

    instruction_shingles: frozenset[tuple[str, ...]]

    @classmethod
    def for_instruction(cls, system_instruction: str) -> DisclosureGuard:
        return cls(_shingles(_words(system_instruction)))

    def check(self, text: str) -> DisclosureReason | None:
        if _CREDENTIAL.search(text):
            return DisclosureReason.CREDENTIAL_PATTERN
        if self.instruction_shingles and not self.instruction_shingles.isdisjoint(
            _shingles(_words(text))
        ):
            return DisclosureReason.INSTRUCTION_REPRODUCED
        return None
