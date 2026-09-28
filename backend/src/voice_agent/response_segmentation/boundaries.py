"""Sentence/phrase boundary detection for Hindi, Hinglish, and English (docs/08 §11, docs/09 §6).

Rules (versioned with the segmenter):

- Devanagari danda/double danda end a sentence immediately, even at the end
  of a streamed delta and without a following space (they are unambiguous);
- Latin ``. ! ?`` end a sentence only when followed by whitespace (or the end
  of a finished stream), so decimals, URLs, and ``3.5`` never split;
- a period after a known abbreviation or a single-letter initial never splits;
- a newline is a natural pause boundary;
- while streaming, a terminator at the very end of the buffer waits for the
  next character.
"""

from __future__ import annotations

DANDA = chr(0x0964)
DOUBLE_DANDA = chr(0x0965)
HARD_TERMINATORS = frozenset({DANDA, DOUBLE_DANDA})
LATIN_TERMINATORS = frozenset({".", "!", "?"})
TRAILING_CLOSERS = frozenset({'"', "'", ")", chr(0x201D), chr(0x2019)})
_OPENERS = "(\"'" + chr(0x201C) + chr(0x2018)
_TERMINAL_RUN = LATIN_TERMINATORS | HARD_TERMINATORS | TRAILING_CLOSERS
CLAUSE_BREAKS = frozenset({",", ";", ":", chr(0x2014), chr(0x2013)})

ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "rs",
        "approx",
        "govt",
        "ltd",
        "pvt",
        "inc",
        "co",
        "a.m",
        "p.m",
        "u.s",
        "fig",
    }
)


def _word_before(text: str, index: int) -> str:
    start = index
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[start:index].lstrip(_OPENERS).lower()


def _is_abbreviation(text: str, period_index: int) -> bool:
    word = _word_before(text, period_index)
    if len(word) == 1 and word.isalpha():
        return True
    return word in ABBREVIATIONS


def _consume_terminal_run(text: str, index: int) -> int:
    end = index + 1
    while end < len(text) and text[end] in _TERMINAL_RUN:
        end += 1
    return end


def _boundary_after(text: str, index: int, *, final: bool) -> int | None:
    char = text[index]
    if char == "\n":
        return index + 1
    if char not in HARD_TERMINATORS and char not in LATIN_TERMINATORS:
        return None
    end = _consume_terminal_run(text, index)
    if char in HARD_TERMINATORS:
        return end
    if end == len(text):
        return end if final else None
    if not text[end].isspace():
        return None
    if char == "." and end == index + 1 and _is_abbreviation(text, index):
        return None
    return end


def find_boundaries(text: str, *, final: bool) -> list[int]:
    """Return exclusive end offsets of each complete sentence/phrase in ``text``."""
    boundaries: list[int] = []
    index = 0
    while index < len(text):
        end = _boundary_after(text, index, final=final)
        if end is None:
            index += 1
            continue
        boundaries.append(end)
        index = end
    return boundaries


def split_oversized(text: str, limit: int) -> list[str] | None:
    """Split ``text`` into pieces of at most ``limit`` characters at safe boundaries.

    Prefers a clause break followed by whitespace, then whitespace. Never
    splits inside a word or number. Returns ``None`` when no safe split exists.
    """
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = _best_cut(remaining, limit)
        if cut is None:
            return None
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _best_cut(text: str, limit: int) -> int | None:
    window = text[: limit + 1]
    for position in range(len(window) - 1, 0, -1):
        if window[position].isspace() and window[position - 1] in CLAUSE_BREAKS:
            return position
    for position in range(len(window) - 1, 0, -1):
        if window[position].isspace():
            return position
    return None
