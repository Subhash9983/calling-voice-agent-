"""Turn-language selection, TTS routing, normalization, and boundary helpers."""

from __future__ import annotations

import pytest

from voice_agent.contracts.enums import ResponseLanguage, TtsLanguageCode
from voice_agent.response_segmentation.boundaries import find_boundaries, split_oversized
from voice_agent.response_segmentation.language import (
    classify_turn_language,
    contains_devanagari,
    route_tts_language,
)
from voice_agent.response_segmentation.normalization import normalize_for_speech
from voice_agent.response_segmentation.validation import validate_segment


@pytest.mark.parametrize(
    ("text", "detected", "expected"),
    [
        ("आप कैसे हैं?", "hi", ResponseLanguage.HINDI),
        ("मुझे order status check करना है", "hi", ResponseLanguage.HINGLISH),
        ("Mujhe order status check karna hai", "hi", ResponseLanguage.HINGLISH),
        ("What is the weather?", "en", ResponseLanguage.ENGLISH),
        ("What is the weather?", "en-IN", ResponseLanguage.ENGLISH),
        ("What is the weather?", None, None),
        ("What is the weather?", "ta", None),
        ("12345", "hi", None),
    ],
)
def test_turn_language_follows_script_and_stt_label(
    text: str, detected: str | None, expected: ResponseLanguage | None
) -> None:
    assert classify_turn_language(text, detected) is expected


@pytest.mark.parametrize(
    ("text", "context", "expected"),
    [
        ("नमस्ते", ResponseLanguage.ENGLISH, TtsLanguageCode.HI_IN),
        ("Theek hai", ResponseLanguage.HINGLISH, TtsLanguageCode.HI_IN),
        ("Sounds good", ResponseLanguage.ENGLISH, TtsLanguageCode.EN_IN),
        ("Sounds good", ResponseLanguage.HINDI, TtsLanguageCode.HI_IN),
        ("Sounds good", None, TtsLanguageCode.HI_IN),
    ],
)
def test_tts_routing_policy(
    text: str, context: ResponseLanguage | None, expected: TtsLanguageCode
) -> None:
    assert route_tts_language(text, context) is expected


def test_devanagari_detection() -> None:
    assert contains_devanagari("abc क")
    assert not contains_devanagari("abc")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("## Heading", "Heading"),
        ("> quoted text", "quoted text"),
        ("1. First step", "First step"),
        ("[docs](https://x.example) are here", "docs are here"),
        ("![logo](https://x.example/l.png) logo", "logo logo"),
        ("`code` and ~~old~~ **new**", "code and old new"),
        ("tabs\tand\nnewlines   collapse", "tabs and newlines collapse"),
        ("```python", ""),
    ],
)
def test_markdown_normalization(raw: str, expected: str) -> None:
    assert normalize_for_speech(raw) == expected


def test_normalization_preserves_numbers_and_codes() -> None:
    text = "OTP 482913 is valid till 28/09/2026, amount ₹1,499.50."

    assert normalize_for_speech(text) == text
    assert validate_segment(text) is None


def test_boundaries_on_final_text_include_terminal_period() -> None:
    assert find_boundaries("Hello there. Bye.", final=True) == [12, 17]
    assert find_boundaries("Hello there. Bye.", final=False) == [12]


def test_split_oversized_returns_none_when_no_safe_boundary() -> None:
    assert split_oversized("x" * 20, 10) is None
    assert split_oversized("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]
