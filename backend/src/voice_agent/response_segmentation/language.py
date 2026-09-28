"""Script detection and TTS language routing (docs/09 §7, docs/10 §3).

- Hindi/Hinglish -> ``hi-IN``; English-only -> ``en-IN``;
- uncertain text -> the accepted user-turn language context, else ``hi-IN``.

Latin-script text alone cannot distinguish English from romanized Hinglish
without a classifier (which needs separate approval, docs/10 §3), so Latin
text follows the turn's language context.
"""

from __future__ import annotations

from voice_agent.contracts.enums import ResponseLanguage, TtsLanguageCode

_DEVANAGARI_RANGES: tuple[tuple[int, int], ...] = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))


def is_devanagari(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _DEVANAGARI_RANGES)


def contains_devanagari(text: str) -> bool:
    return any(is_devanagari(ch) for ch in text)


def route_tts_language(text: str, turn_language: ResponseLanguage | None) -> TtsLanguageCode:
    if contains_devanagari(text):
        return TtsLanguageCode.HI_IN
    if turn_language is ResponseLanguage.ENGLISH:
        return TtsLanguageCode.EN_IN
    return TtsLanguageCode.HI_IN


def _has_latin_letters(text: str) -> bool:
    return any(ch.isascii() and ch.isalpha() for ch in text)


def classify_turn_language(text: str, detected: str | None) -> ResponseLanguage | None:
    """Select the turn language from the accepted transcript (docs/10 §3).

    Devanagari only -> Hindi; Devanagari plus Latin words -> Hinglish; Latin
    only follows the STT language label (``en`` -> English, ``hi`` ->
    romanized Hinglish). Without script or label evidence it stays unknown.
    """
    has_devanagari = contains_devanagari(text)
    has_latin = _has_latin_letters(text)
    if has_devanagari:
        return ResponseLanguage.HINGLISH if has_latin else ResponseLanguage.HINDI
    if not has_latin or detected is None:
        return None
    label = detected.lower()
    if label.startswith("en"):
        return ResponseLanguage.ENGLISH
    if label.startswith("hi"):
        return ResponseLanguage.HINGLISH
    return None
