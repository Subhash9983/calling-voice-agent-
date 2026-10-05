"""Deterministic pre-TTS text preparation for one speakable segment (docs/09 §6, §8).

Runs after the ResponseSegmenter's markup normalization and before any TTS
request. Every rule preserves meaning; nothing is translated, transliterated,
or invented (docs/09 §8):

- currency amounts (``Rs``/``Rs.``/``INR``/``₹`` and a following
  ``rupees``/``रुपये``) are normalized to ``₹`` and digit-grouped (Indian
  grouping), as Sarvam asks for commas in numbers above four digits; ``$``
  amounts use international grouping;
- bare long digit runs (OTPs, phone numbers, codes) are never regrouped,
  because their digit-by-digit reading is the meaning;
- a small fixed table of English abbreviations expands (``e.g.``, ``i.e.``,
  ``etc.``, ``vs.``, ``Dr.``, ``Mr.``, ``Mrs.``, ``approx.``, ``No.`` before a
  digit), and ``&`` becomes ``and`` only for ``en-IN`` routing;
- repeated punctuation collapses and dashes become comma pauses;
- the result is split at safe boundaries into pieces of at most 500 Unicode
  characters (expansion can lengthen text), with a word-boundary hard cut as
  the last resort.

The original text is kept separately; :data:`TTS_TEXT_VERSION` is recorded
as evidence so any behaviour change is a new version.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.tts import MAX_TTS_SEGMENT_CHARS
from voice_agent.response_segmentation.boundaries import split_oversized
from voice_agent.response_segmentation.normalization import normalize_for_speech

TTS_TEXT_VERSION: Final = "phase0_tts_text_v1"
RUPEE: Final = "₹"
_INDIAN_LEAD_DIGITS: Final = 3

_AMOUNT = r"(\d[\d,]*)(\.\d+)?"
_RUPEE_PREFIX = re.compile(rf"(?<![\w{RUPEE}])(?:rs\.?|inr|{RUPEE})\s?{_AMOUNT}", re.IGNORECASE)
_DOLLAR_PREFIX = re.compile(rf"(?<![\w$])\$\s?{_AMOUNT}")
_RUPEE_SUFFIX = re.compile(
    r"(?<![\d,.])(\d{5,9})(?=\s?(?:rupees?|rupaye|rupaiye|रुपये"
    r"|रुपए|रुपया)(?!\w))",
    re.IGNORECASE,
)
_ABBREVIATIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?<!\w)e\.g\.(?!\w)", re.IGNORECASE), "for example"),
    (re.compile(r"(?<!\w)i\.e\.(?!\w)", re.IGNORECASE), "that is"),
    (re.compile(r"(?<!\w)etc\.(?!\w)", re.IGNORECASE), "etcetera"),
    (re.compile(r"(?<!\w)approx\.(?!\w)", re.IGNORECASE), "approximately"),
    (re.compile(r"(?<!\w)vs\.?(?!\w)", re.IGNORECASE), "versus"),
    (re.compile(r"(?<!\w)Dr\.(?=\s)"), "Doctor"),
    (re.compile(r"(?<!\w)Mrs\.(?=\s)"), "Missus"),
    (re.compile(r"(?<!\w)Mr\.(?=\s)"), "Mister"),
    (re.compile(r"(?<!\w)No\.(?=\s?\d)"), "number"),
)
_AMPERSAND = re.compile(r"\s&\s")
_REPEATED_TERMINAL = re.compile(r"([!?])[!?]+")
_LONG_ELLIPSIS = re.compile(r"\.{4,}")
_EN_DASH, _EM_DASH = chr(0x2013), chr(0x2014)
_DASH_PAUSE = re.compile(rf"\s+[-{_EN_DASH}{_EM_DASH}]+\s+|\s*{_EM_DASH}\s*")
_SPACE_BEFORE_COMMA = re.compile(r"\s+,")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TtsTextPlan:
    original: str
    pieces: tuple[str, ...]
    language_code: TtsLanguageCode
    version: str = TTS_TEXT_VERSION

    @property
    def normalized(self) -> str:
        return " ".join(self.pieces)

    @property
    def is_empty(self) -> bool:
        return not self.pieces


def group_indian(digits: str) -> str:
    """``1250000`` -> ``12,50,000`` (last three digits, then pairs)."""
    if len(digits) <= _INDIAN_LEAD_DIGITS:
        return digits
    head, tail = digits[:-_INDIAN_LEAD_DIGITS], digits[-_INDIAN_LEAD_DIGITS:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *pairs, tail])


def _group_international(digits: str) -> str:
    return f"{int(digits):,}"


def _amount(grouper: Callable[[str], str], prefix: str) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        integer, decimals = match.group(1), match.group(2) or ""
        if "," in integer:  # already grouped by the author; keep it
            return f"{prefix}{integer}{decimals}"
        return f"{prefix}{grouper(integer)}{decimals}"

    return replace


def _currency(text: str) -> str:
    text = _RUPEE_PREFIX.sub(_amount(group_indian, RUPEE), text)
    text = _DOLLAR_PREFIX.sub(_amount(_group_international, "$"), text)
    return _RUPEE_SUFFIX.sub(lambda m: group_indian(m.group(1)), text)


def _abbreviations(text: str, language: TtsLanguageCode) -> str:
    for pattern, expansion in _ABBREVIATIONS:
        text = pattern.sub(expansion, text)
    if language is TtsLanguageCode.EN_IN:
        text = _AMPERSAND.sub(" and ", text)
    return text


def _punctuation(text: str) -> str:
    text = _REPEATED_TERMINAL.sub(r"\1", text)
    text = _LONG_ELLIPSIS.sub("...", text)
    text = _DASH_PAUSE.sub(", ", text)
    return _SPACE_BEFORE_COMMA.sub(",", text)


def _speakable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _hard_cut(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind(" ", 0, limit + 1)
        cut = cut if cut > 0 else limit
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [*pieces, remaining] if remaining else pieces


def _split(text: str, limit: int) -> tuple[str, ...]:
    if len(text) <= limit:
        pieces = [text]
    else:
        pieces = split_oversized(text, limit) or _hard_cut(text, limit)
    return tuple(piece for piece in pieces if piece and _speakable(piece))


def prepare_tts_text(
    text: str, language: TtsLanguageCode, *, max_chars: int = MAX_TTS_SEGMENT_CHARS
) -> TtsTextPlan:
    """The text actually submitted to TTS for one segment, split within the cap."""
    result = normalize_for_speech(text)
    result = _currency(result)
    result = _abbreviations(result, language)
    result = _punctuation(result)
    result = _WHITESPACE.sub(" ", result).strip()
    return TtsTextPlan(original=text, pieces=_split(result, max_chars), language_code=language)
