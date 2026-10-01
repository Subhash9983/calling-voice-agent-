"""Deepgram option mapping and message normalization (offline, synthetic fixtures)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.stt_adapters.deepgram.messages import (
    MalformedMessage,
    MetadataMessage,
    ResultsMessage,
    SpeechStartedMessage,
    UnknownMessage,
    UtteranceEndMessage,
    parse_message,
)
from voice_agent.stt_adapters.deepgram.options import (
    DEEPGRAM_MODEL,
    KeytermRejectedError,
    UnsupportedSttConfigError,
    build_stream_options,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "deepgram" / "stream_messages.json"


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return dict(loaded[name])


# ------------------------------------------------------------------ options --
def test_baseline_maps_to_nova_3_multilingual_linear16_with_no_keyterms() -> None:
    options = build_stream_options(SttStreamConfig(), model=DEEPGRAM_MODEL)

    assert dict(options.params) == {
        "model": "nova-3",
        "language": "multi",
        "encoding": "linear16",
        "sample_rate": "16000",
        "channels": "1",
        "interim_results": "true",
        "punctuate": "true",
        "smart_format": "true",
        "mip_opt_out": "true",
    }
    assert options.keyterm_count == 0
    assert "keyterm" not in options.params


def test_every_stream_opts_out_of_the_model_improvement_program() -> None:
    config = SttStreamConfig(partial_transcripts=False, punctuation=False, smart_formatting=False)

    params = build_stream_options(config, model=DEEPGRAM_MODEL).params

    assert params["mip_opt_out"] == "true"


def test_partials_and_formatting_follow_the_normalized_flags() -> None:
    config = SttStreamConfig(partial_transcripts=False, punctuation=False, smart_formatting=False)

    params = build_stream_options(config, model=DEEPGRAM_MODEL).params

    assert params["interim_results"] == "false"
    assert params["punctuate"] == "false"
    assert params["smart_format"] == "false"


def test_approved_keyterms_map_to_keyterm_prompting_with_mocks_only() -> None:
    config = SttStreamConfig(keyterms=("NiaLabs", "IvyPrints", "Schoollog"))

    options = build_stream_options(config, model=DEEPGRAM_MODEL, keyterms_approved=True)

    assert options.params["keyterm"] == ("NiaLabs", "IvyPrints", "Schoollog")
    assert options.keyterm_count == 3


def test_keyterms_without_paid_feature_approval_are_rejected_not_dropped() -> None:
    config = SttStreamConfig(keyterms=("NiaLabs",))

    with pytest.raises(KeytermRejectedError) as raised:
        build_stream_options(config, model=DEEPGRAM_MODEL)

    assert raised.value.reason == "keyterms_not_approved"


def test_keyterms_on_a_model_without_keyterm_support_are_rejected() -> None:
    config = SttStreamConfig(keyterms=("NiaLabs",))

    with pytest.raises(KeytermRejectedError) as raised:
        build_stream_options(config, model="nova-2", keyterms_approved=True)

    assert raised.value.reason == "keyterms_unsupported_by_model"


@pytest.mark.parametrize(
    "config",
    [
        SttStreamConfig(sample_rate_hz=48_000),
        SttStreamConfig(expected_languages=("hi",)),
        SttStreamConfig(code_switching=False),
    ],
)
def test_unsupported_configuration_is_rejected(config: SttStreamConfig) -> None:
    with pytest.raises(UnsupportedSttConfigError):
        build_stream_options(config, model=DEEPGRAM_MODEL)


# ----------------------------------------------------------------- messages --
def test_interim_result_is_normalized_without_sdk_types() -> None:
    message = parse_message(fixture("interim_hinglish"))

    assert isinstance(message, ResultsMessage)
    assert not message.is_final
    assert message.transcript == "मेरा नाम"
    assert [w.text for w in message.words] == ["मेरा", "नाम"]
    assert message.request_id == "00000000-0000-4000-8000-00000000d001"


def test_final_result_keeps_script_languages_and_word_timing() -> None:
    message = parse_message(fixture("final_hinglish"))

    assert isinstance(message, ResultsMessage)
    assert message.is_final
    assert message.speech_final
    assert not message.from_finalize
    assert message.transcript == "मेरा नाम Arun है।"
    assert message.languages == ("hi", "en")
    assert message.words[2].start_s == pytest.approx(0.7)
    assert message.words[2].language == "hi"


def test_empty_result_has_no_invented_language_or_confidence() -> None:
    message = parse_message(fixture("empty_from_finalize"))

    assert isinstance(message, ResultsMessage)
    assert message.from_finalize
    assert message.transcript == ""
    assert message.languages == ()
    assert message.confidence is None


def test_metadata_speech_started_utterance_end_unknown_and_malformed() -> None:
    metadata = parse_message(fixture("metadata"))

    assert isinstance(metadata, MetadataMessage)
    assert metadata.duration_s == pytest.approx(8.0)
    assert isinstance(parse_message(fixture("speech_started")), SpeechStartedMessage)
    assert isinstance(parse_message(fixture("utterance_end")), UtteranceEndMessage)
    unknown = parse_message(fixture("unknown"))
    assert isinstance(unknown, UnknownMessage)
    assert unknown.message_type == "SomethingNew"
    malformed = parse_message(fixture("malformed_results"))
    assert isinstance(malformed, MalformedMessage)
    assert "not-a-list" not in repr(malformed)


def test_oversized_or_non_mapping_messages_are_malformed() -> None:
    oversized = fixture("final_hinglish")
    oversized["channel"]["alternatives"][0]["transcript"] = "x" * 20_000

    assert isinstance(parse_message(oversized), MalformedMessage)
    assert isinstance(parse_message({"no": "type"}), UnknownMessage)
