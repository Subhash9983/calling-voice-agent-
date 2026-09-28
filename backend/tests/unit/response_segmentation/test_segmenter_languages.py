"""Response segmentation for Hindi (Devanagari), Hinglish, and English.

docs/08 §11, docs/09 §6-§8, docs/10 §3 and §8.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from voice_agent.contracts.enums import FinishReason, ResponseLanguage, TtsLanguageCode
from voice_agent.response_segmentation.segmenter import (
    DiscardedTail,
    RejectedSegment,
    SegmenterState,
    SegmentOutcome,
    SpeakableSegment,
    feed,
    finish,
    speakable,
)
from voice_agent.response_segmentation.validation import SegmentRejectionReason


def _stream(
    deltas: Sequence[str],
    language: ResponseLanguage | None,
    finish_reason: FinishReason = FinishReason.COMPLETED,
) -> tuple[list[list[SegmentOutcome]], list[SegmentOutcome], SegmenterState]:
    state = SegmenterState(turn_language=language)
    per_delta: list[list[SegmentOutcome]] = []
    for delta in deltas:
        state, outcomes = feed(state, delta)
        per_delta.append(outcomes)
    state, tail = finish(state, finish_reason)
    return per_delta, tail, state


def _texts(outcomes: Sequence[SegmentOutcome]) -> list[str]:
    return [o.text for o in outcomes if isinstance(o, SpeakableSegment)]


# ----------------------------------------------------------------- Hindi ----


def test_hindi_danda_ends_a_sentence_without_waiting_for_the_stream() -> None:
    per_delta, tail, _ = _stream(
        ["नमस्ते! मैं आपकी ", "मदद कर सकती हूँ। आप ", "क्या जानना चाहते हैं?"],
        ResponseLanguage.HINDI,
    )

    assert _texts(per_delta[0]) == ["नमस्ते!"]
    assert _texts(per_delta[1]) == ["मैं आपकी मदद कर सकती हूँ।"]
    assert _texts(tail) == ["आप क्या जानना चाहते हैं?"]


def test_hindi_danda_splits_even_without_a_following_space() -> None:
    per_delta, _, _ = _stream(["पहला वाक्य।दूसरा"], ResponseLanguage.HINDI)

    assert _texts(per_delta[0]) == ["पहला वाक्य।"]


def test_hindi_routes_to_hi_in_and_preserves_devanagari_joiners() -> None:
    zwj_word = "क्‍ष"
    per_delta, tail, _ = _stream([f"{zwj_word} ठीक है।"], ResponseLanguage.HINDI)

    (segment,) = speakable(per_delta[0])
    assert segment.language_code is TtsLanguageCode.HI_IN
    assert "‍" in segment.text
    assert tail == []


def test_hindi_double_danda_and_trailing_tail_on_completion() -> None:
    per_delta, tail, _ = _stream(["धन्यवाद॥ फिर मिलेंगे"], ResponseLanguage.HINDI)

    assert _texts(per_delta[0]) == ["धन्यवाद॥"]
    assert _texts(tail) == ["फिर मिलेंगे"]


# -------------------------------------------------------------- Hinglish ----


def test_mixed_script_hinglish_keeps_code_mixed_text_together() -> None:
    per_delta, tail, _ = _stream(
        ["Sorry, मुझे आपकी बात clear नहीं हुई। ", "क्या आप एक बार फिर कह सकते हैं?"],
        ResponseLanguage.HINGLISH,
    )

    assert _texts(per_delta[0]) == ["Sorry, मुझे आपकी बात clear नहीं हुई।"]
    assert _texts(tail) == ["क्या आप एक बार फिर कह सकते हैं?"]
    assert {s.language_code for s in speakable(per_delta[0] + tail)} == {TtsLanguageCode.HI_IN}


def test_romanized_hinglish_segments_on_latin_punctuation_and_routes_hi_in() -> None:
    per_delta, tail, _ = _stream(
        ["Aapka order kal tak ", "aa jayega. Kya main ", "aur kuch help karun?"],
        ResponseLanguage.HINGLISH,
    )

    assert _texts(per_delta[1]) == ["Aapka order kal tak aa jayega."]
    assert _texts(tail) == ["Kya main aur kuch help karun?"]
    assert all(s.language_code is TtsLanguageCode.HI_IN for s in speakable(per_delta[1] + tail))


def test_romanized_hinglish_does_not_split_rupee_amounts_or_decimals() -> None:
    per_delta, tail, _ = _stream(
        ["Iska price Rs. 1,499.50 hai. Theek hai?"], ResponseLanguage.HINGLISH
    )

    assert _texts(per_delta[0]) == ["Iska price Rs. 1,499.50 hai."]
    assert _texts(tail) == ["Theek hai?"]


# --------------------------------------------------------------- English ----


def test_english_waits_for_whitespace_before_splitting_on_a_period() -> None:
    state = SegmenterState(turn_language=ResponseLanguage.ENGLISH)
    state, first = feed(state, "The value is 3.")
    state, second = feed(state, "5 percent. Next")

    assert first == []
    assert _texts(second) == ["The value is 3.5 percent."]
    assert state.buffer == " Next"


@pytest.mark.parametrize(
    "text",
    [
        "Dr. Sharma will call you at 10 a.m. tomorrow.",
        "Please bring your ID, e.g. a passport, to the office.",
        "The U.S. office opens soon.",
        "Mr. A. Kumar signed it.",
    ],
)
def test_english_abbreviations_and_initials_do_not_split(text: str) -> None:
    per_delta, tail, _ = _stream([text + " Thanks!"], ResponseLanguage.ENGLISH)

    assert _texts(per_delta[0] + tail) == [text, "Thanks!"]


def test_english_routes_to_en_in_only_with_english_turn_context() -> None:
    english, _, _ = _stream(["Hello there. "], ResponseLanguage.ENGLISH)
    unknown, _, _ = _stream(["Hello there. "], None)

    assert speakable(english[0])[0].language_code is TtsLanguageCode.EN_IN
    assert speakable(unknown[0])[0].language_code is TtsLanguageCode.HI_IN


def test_markdown_list_is_normalized_but_raw_text_is_preserved() -> None:
    per_delta, tail, state = _stream(
        ["**Steps:**\n", "- Open the app\n", "- Tap *Settings*"], ResponseLanguage.ENGLISH
    )

    outcomes = [o for batch in per_delta for o in batch] + tail
    assert _texts(outcomes) == ["Steps:", "Open the app", "Tap Settings"]
    assert state.generated_text == "**Steps:**\n- Open the app\n- Tap *Settings*"


def test_emoji_and_invisible_characters_are_removed() -> None:
    _, tail, _ = _stream(["Great job \U0001f389​!"], ResponseLanguage.ENGLISH)

    assert _texts(tail) == ["Great job !"]


# ------------------------------------------------------ Validation paths ----


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Visit https://example.com for details.", SegmentRejectionReason.RAW_URL),
        ("See www.example.org now.", SegmentRejectionReason.RAW_URL),
        ("(laughs) That is funny.", SegmentRejectionReason.STAGE_DIRECTION),
        ("My system prompt says hello.", SegmentRejectionReason.PROMPT_DISCLOSURE),
        ("Use the {placeholder} value.", SegmentRejectionReason.UNSUPPORTED_MARKUP),
        ("<b>Bold</b> answer.", SegmentRejectionReason.UNSUPPORTED_MARKUP),
    ],
)
def test_unsafe_segments_are_rejected_not_spoken(text: str, reason: SegmentRejectionReason) -> None:
    _, tail, _ = _stream([text], ResponseLanguage.ENGLISH)

    (outcome,) = tail
    assert isinstance(outcome, RejectedSegment)
    assert outcome.reason is reason


def test_punctuation_only_unit_is_skipped() -> None:
    _, tail, _ = _stream(["... "], ResponseLanguage.ENGLISH)

    assert [o for o in tail if isinstance(o, SpeakableSegment)] == []


# ------------------------------------------------ Length and truncation ----


def test_oversized_sentence_splits_at_a_safe_clause_boundary() -> None:
    clause = "यह एक लंबा वाक्यांश है"
    long_sentence = ", ".join([clause] * 30) + "।"

    per_delta, tail, _ = _stream([long_sentence], ResponseLanguage.HINDI)

    texts = _texts(per_delta[0] + tail)
    assert len(texts) >= 2
    assert all(len(text) <= 500 for text in texts)
    assert " ".join(texts).replace(" ,", ",") == long_sentence
    assert all(not text.startswith(",") for text in texts)


def test_unfinished_phrase_longer_than_cap_is_force_split_while_streaming() -> None:
    words = " ".join(["word"] * 150)

    state = SegmenterState(turn_language=ResponseLanguage.ENGLISH)
    state, outcomes = feed(state, words)

    assert outcomes, "a >500-character unfinished phrase must not block TTS"
    assert all(len(text) <= 500 for text in _texts(outcomes))
    assert len(state.buffer) <= 500


def test_unsplittable_oversized_text_is_rejected() -> None:
    _, tail, _ = _stream(["x" * 600 + "."], ResponseLanguage.ENGLISH)

    assert isinstance(tail[0], RejectedSegment)
    assert tail[0].reason is SegmentRejectionReason.TOO_LONG


def test_maximum_tokens_discards_the_incomplete_tail() -> None:
    per_delta, tail, _ = _stream(
        ["पहला वाक्य पूरा है। ", "दूसरा अधूरा"], ResponseLanguage.HINDI, FinishReason.MAXIMUM_TOKENS
    )

    assert _texts(per_delta[0]) == ["पहला वाक्य पूरा है।"]
    assert _texts(tail) == []
    (discarded,) = tail
    assert isinstance(discarded, DiscardedTail)
    assert discarded.raw_text.strip() == "दूसरा अधूरा"


def test_maximum_tokens_keeps_a_terminated_final_sentence() -> None:
    _, tail, _ = _stream(["Done now."], ResponseLanguage.ENGLISH, FinishReason.MAXIMUM_TOKENS)

    assert _texts(tail) == ["Done now."]


def test_error_finish_never_releases_buffered_text() -> None:
    _, tail, _ = _stream(["Half a thought"], ResponseLanguage.ENGLISH, FinishReason.ERROR)

    assert tail == [DiscardedTail("Half a thought")]


def test_finished_segmenter_rejects_more_text_and_finish_is_idempotent() -> None:
    _, _, state = _stream(["Hi."], ResponseLanguage.ENGLISH)

    with pytest.raises(ValueError, match="finished"):
        feed(state, "more")
    assert finish(state, FinishReason.COMPLETED) == (state, [])


def test_sequences_are_monotonic_across_deltas() -> None:
    per_delta, tail, _ = _stream(["One. Two. ", "Three."], ResponseLanguage.ENGLISH)

    outcomes = [o for batch in per_delta for o in batch] + tail
    assert [o.sequence for o in outcomes if isinstance(o, SpeakableSegment)] == [0, 1, 2]


def test_generated_text_is_capped() -> None:
    state = SegmenterState(turn_language=ResponseLanguage.ENGLISH)
    state, _ = feed(state, "a " * 10_001)

    assert len(state.generated_text) == 20_000
    assert state.overflowed
