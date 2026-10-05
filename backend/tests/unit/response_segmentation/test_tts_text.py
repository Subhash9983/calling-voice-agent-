"""Pre-TTS text preparation (docs/09 §6, §8): meaning-preserving, bounded, versioned.

Text-level Hindi, Hinglish, and Indian-English cases only; no audio judgment.
"""

from __future__ import annotations

import pytest

from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.tts import MAX_TTS_SEGMENT_CHARS
from voice_agent.response_segmentation.tts_text import (
    TTS_TEXT_VERSION,
    TtsTextPlan,
    group_indian,
    prepare_tts_text,
)

HI = TtsLanguageCode.HI_IN
EN = TtsLanguageCode.EN_IN


def _text(text: str, language: TtsLanguageCode = HI) -> str:
    return prepare_tts_text(text, language).normalized


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Pure Hindi stays native Devanagari, untouched (no transliteration/translation).
        ("नमस्ते! मैं आपकी कैसे मदद कर सकती हूँ?", "नमस्ते! मैं आपकी कैसे मदद कर सकती हूँ?"),
        # Hinglish code-mixing stays coherent.
        ("Aapka order kal deliver ho jayega.", "Aapka order kal deliver ho jayega."),
        ("आपका OTP 482913 है।", "आपका OTP 482913 है।"),
        # Currency amounts get Indian digit grouping; the symbol is normalized.
        ("Fees Rs. 125000 hai.", "Fees ₹1,25,000 hai."),
        ("Total INR 2500 only.", "Total ₹2,500 only."),
        ("कीमत ₹ 1500000 है।", "कीमत ₹15,00,000 है।"),
        ("Price is rs 99.50 today.", "Price is ₹99.50 today."),
        ("It costs 45000 rupees.", "It costs 45,000 rupees."),
        ("इसकी कीमत 350000 रुपये है।", "इसकी कीमत 3,50,000 रुपये है।"),
        ("Pay $1250000 now.", "Pay $1,250,000 now."),
        # Already grouped numbers, decimals, percentages, and times are kept.
        (
            "Rs 1,25,000 aur 12.5% discount, 10:30 baje.",
            "₹1,25,000 aur 12.5% discount, 10:30 baje.",
        ),
        # Bare long numbers are ambiguous (OTP/phone) and are never regrouped.
        ("Call 9876543210 or use code 123456.", "Call 9876543210 or use code 123456."),
    ],
)
def test_language_and_number_cases(raw: str, expected: str) -> None:
    assert _text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bring ID, e.g. Aadhaar.", "Bring ID, for example Aadhaar."),
        ("Docs i.e. PAN etc. are needed.", "Docs that is PAN etcetera are needed."),
        ("Dr. Sharma vs. Mr. Rao", "Doctor Sharma versus Mister Rao"),
        ("Room No. 5 is approx. 20 m away.", "Room number 5 is approximately 20 m away."),
        ("Tea & coffee", "Tea and coffee"),
    ],
)
def test_english_abbreviations_expand(raw: str, expected: str) -> None:
    assert _text(raw, EN) == expected


def test_ampersand_is_kept_in_hindi_routing() -> None:
    assert _text("चाय & कॉफ़ी", HI) == "चाय & कॉफ़ी"


def test_abbreviation_lookalikes_are_not_rewritten() -> None:
    assert _text("Medr. etcher Nominee", EN) == "Medr. etcher Nominee"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wow!!! Really???", "Wow! Really?"),
        ("Hmm.... okay?!", "Hmm... okay?"),
        ("Pehle yeh — phir woh - bas", "Pehle yeh, phir woh, bas"),
        ("**Note:** yeh _zaroori_ hai 😀", "Note: yeh _zaroori_ hai"),
        ("  extra   spaces\n\nhere  ", "extra spaces here"),
    ],
)
def test_punctuation_and_markup(raw: str, expected: str) -> None:
    assert _text(raw) == expected


def test_plan_keeps_original_separate_and_records_version() -> None:
    plan = prepare_tts_text("Fees Rs. 125000 hai.", HI)

    assert isinstance(plan, TtsTextPlan)
    assert plan.original == "Fees Rs. 125000 hai."
    assert plan.normalized != plan.original
    assert plan.version == TTS_TEXT_VERSION
    assert plan.language_code is HI


def test_empty_or_unspeakable_text_has_no_pieces() -> None:
    assert prepare_tts_text("", HI).pieces == ()
    assert prepare_tts_text("  ... ?! 😀 ", HI).pieces == ()
    assert prepare_tts_text("", HI).is_empty


def test_oversized_text_splits_at_safe_boundaries_within_the_cap() -> None:
    sentence = "Yeh ek lamba vaakya hai jo baar baar dohraya ja raha hai, "
    plan = prepare_tts_text(sentence * 20, HI)

    assert len(plan.pieces) >= 2
    assert all(0 < len(piece) <= MAX_TTS_SEGMENT_CHARS for piece in plan.pieces)
    assert " ".join(plan.pieces).split() == plan.normalized.split()


def test_expansion_past_the_cap_is_split_again() -> None:
    raw = ("e.g. " * 99).strip()
    plan = prepare_tts_text(raw, EN)

    assert all(len(piece) <= MAX_TTS_SEGMENT_CHARS for piece in plan.pieces)
    assert len(plan.pieces) >= 2


def test_unsplittable_oversized_text_is_hard_cut_on_a_word_boundary() -> None:
    plan = prepare_tts_text("a" * 600, HI)

    assert all(len(piece) <= MAX_TTS_SEGMENT_CHARS for piece in plan.pieces)
    assert "".join(plan.pieces) == "a" * 600


def test_normalization_is_idempotent() -> None:
    once = _text("Fees Rs. 125000, e.g. abhi!!!", EN)

    assert _text(once, EN) == once


@pytest.mark.parametrize(
    ("digits", "expected"),
    [("5", "5"), ("1000", "1,000"), ("100000", "1,00,000"), ("12345678", "1,23,45,678")],
)
def test_indian_grouping(digits: str, expected: str) -> None:
    assert group_indian(digits) == expected
