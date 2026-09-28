"""Deterministic pre-TTS speakability normalization (docs/09 §8, docs/10 §8).

May remove Markdown/control/list syntax, emoji, and invisible characters and
collapse whitespace. It never adds facts, translates, or rewrites meaning.
Devanagari ZWJ/ZWNJ (conjunct control) are preserved.
"""

from __future__ import annotations

import re
import unicodedata

from voice_agent.response_segmentation.language import is_devanagari

NORMALIZATION_VERSION = "phase0_speech_normalization_v1"

ZWNJ = chr(0x200C)
ZWJ = chr(0x200D)

_CODE_FENCE = re.compile(r"```[^\n`]*")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BLOCKQUOTE = re.compile(r"^\s*>\s?")
_BULLET = re.compile(r"^\s*[-*+\u2022]\s+")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+")
_EMPHASIS = re.compile(r"(\*\*|__|~~)(.+?)\1")
_SINGLE_EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRAY_MARKUP = re.compile(r"[*`~]+")
_WHITESPACE = re.compile(r"\s+")
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F1E6, 0x1F1FF),
)


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def _keep_joiner(text: str, index: int) -> bool:
    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return bool(before and after and is_devanagari(before) and is_devanagari(after))


def _strip_invisible_and_emoji(text: str) -> str:
    kept: list[str] = []
    for index, char in enumerate(text):
        if _is_emoji(char):
            continue
        if char in {ZWJ, ZWNJ}:
            if _keep_joiner(text, index):
                kept.append(char)
            continue
        category = unicodedata.category(char)
        if category == "Cc":
            kept.append(" " if char in "\t\n\r" else "")
            continue
        if category == "Cf":
            continue
        kept.append(char)
    return "".join(kept)


def _strip_line_markup(line: str) -> str:
    for pattern in (_HEADING, _BLOCKQUOTE, _BULLET, _NUMBERED):
        line = pattern.sub("", line)
    return line


def normalize_for_speech(text: str) -> str:
    """Return the text to submit to TTS (the raw text is preserved separately)."""
    result = _CODE_FENCE.sub(" ", text)
    result = _IMAGE.sub(r"\1", result)
    result = _LINK.sub(r"\1", result)
    result = "\n".join(_strip_line_markup(line) for line in result.split("\n"))
    result = _EMPHASIS.sub(r"\2", result)
    result = _SINGLE_EMPHASIS.sub(r"\1", result)
    result = _STRAY_MARKUP.sub("", result)
    result = unicodedata.normalize("NFC", result)
    result = _strip_invisible_and_emoji(result)
    return _WHITESPACE.sub(" ", result).strip()
