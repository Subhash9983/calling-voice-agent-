"""Deterministic segment validation before any TTS request (docs/08 §11, docs/10 §8).

A rejected segment follows a normalized failure path; it is never spoken
blindly and never repaired by inventing text.
"""

from __future__ import annotations

import re
from enum import StrEnum

from voice_agent.contracts.tts import MAX_TTS_SEGMENT_CHARS


class SegmentRejectionReason(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    RAW_URL = "raw_url"
    UNSUPPORTED_MARKUP = "unsupported_markup"
    STAGE_DIRECTION = "stage_direction"
    PROMPT_DISCLOSURE = "prompt_disclosure"


_URL = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(?:com|in|org|net|io|ai|co|dev|app)\b)", re.IGNORECASE
)
_MARKUP = re.compile(r"(<[^>]+>|[\[\]{}|]|^#)")
_STAGE_DIRECTION = re.compile(
    r"\((?:laughs?|laughing|sighs?|pauses?|smiles?|chuckles?|music|applause|clears throat)\)",
    re.IGNORECASE,
)
_PROMPT_DISCLOSURE = re.compile(
    r"(system prompt|system instruction|developer message|# ?role\b|hidden rules)",
    re.IGNORECASE,
)


def _has_speakable_content(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def validate_segment(text: str) -> SegmentRejectionReason | None:
    """Return ``None`` for a valid speakable segment, else the rejection reason."""
    if not _has_speakable_content(text):
        return SegmentRejectionReason.EMPTY
    if len(text) > MAX_TTS_SEGMENT_CHARS:
        return SegmentRejectionReason.TOO_LONG
    if _PROMPT_DISCLOSURE.search(text):
        return SegmentRejectionReason.PROMPT_DISCLOSURE
    if _URL.search(text):
        return SegmentRejectionReason.RAW_URL
    if _STAGE_DIRECTION.search(text):
        return SegmentRejectionReason.STAGE_DIRECTION
    if _MARKUP.search(text):
        return SegmentRejectionReason.UNSUPPORTED_MARKUP
    return None
