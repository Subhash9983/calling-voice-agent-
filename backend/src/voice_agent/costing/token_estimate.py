"""Conservative token estimate before a request (docs/08 §8, §23).

The exact tokenizer is a deferred decision (docs/08 §23), so history budgeting
and estimated usage for unfinished attempts use a deliberately high estimate:
one token per two UTF-8 bytes, plus a fixed per-message overhead. English
(about four bytes per real token) is over-counted roughly twice; Devanagari
(three bytes per character) is counted at about 0.7 tokens per character.
Provider-returned billed counts remain authoritative for cost evidence.
"""

from __future__ import annotations

import math
from typing import Final

BYTES_PER_ESTIMATED_TOKEN: Final = 2
MESSAGE_OVERHEAD_TOKENS: Final = 4


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_ESTIMATED_TOKEN)


def estimate_message_tokens(text: str) -> int:
    return estimate_text_tokens(text) + MESSAGE_OVERHEAD_TOKENS
