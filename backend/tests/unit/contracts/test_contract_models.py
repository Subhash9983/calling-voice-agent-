"""Port contracts: strictness, bounds, Phase 0 restrictions, and failure taxonomy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from voice_agent.contracts.audio import (
    TTS_SAMPLE_RATE_HZ,
    VAD_SAMPLE_RATE_HZ,
    AudioFrame,
    peak_amplitude,
    square_wave_pcm,
)
from voice_agent.contracts.conversation import ConversationRequest, HistoryMessage
from voice_agent.contracts.cost import Currency, RateCard, UnitRate
from voice_agent.contracts.failures import (
    ERROR_CATEGORY_BY_TYPE,
    ErrorCategory,
    ErrorComponent,
    ErrorType,
    NormalizedFailure,
    NormalizedFailureError,
)
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.policies import QueueLimits, RetryPolicy, TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind
from voice_agent.contracts.stt import SttEvent, SttStreamConfig, SttTurnFinalized
from voice_agent.contracts.transport import PlaybackAck, PlaybackAckKind
from voice_agent.contracts.tts import TtsAudioChunk, TtsSegmentRequest, TtsVoiceConfig
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
OPERATION = "00000000-0000-4000-8000-000000000003"
SEGMENT = "00000000-0000-4000-8000-000000000004"
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _stamp(**overrides: Any) -> GenerationStamp:
    values: dict[str, Any] = {
        "session_id": SESSION,
        "turn_id": TURN,
        "operation_id": OPERATION,
        "worker_generation": 1,
        "cancellation_generation": 0,
    }
    values.update(overrides)
    return GenerationStamp(**values)


def _request(**overrides: Any) -> ConversationRequest:
    values: dict[str, Any] = {
        "stamp": _stamp(),
        "logical_request_id": OPERATION,
        "correlation_id": "corr",
        "agent_config_id": SESSION,
        "config_checksum": "sha256:x",
        "system_instruction_id": "phase0_general_voice_assistant_v1",
        "system_instruction_version": 1,
        "system_instruction": "You are a friendly assistant.",
        "user_transcript": "Namaste",
    }
    values.update(overrides)
    return ConversationRequest(**values)


# ------------------------------------------------------------------ audio ---


def test_audio_frame_requires_consistent_mono_pcm() -> None:
    pcm = square_wave_pcm(0.5, VAD_SAMPLE_RATE_HZ // 50)
    frame = AudioFrame(
        session_id=SESSION,
        sequence=0,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=0,
        pcm=pcm,
    )

    assert frame.ends_at_ms == 20
    assert frame.sample_count == 320
    assert peak_amplitude(frame.pcm) == pytest.approx(0.5, abs=1e-4)
    with pytest.raises(ValidationError, match="PCM byte length"):
        AudioFrame(
            session_id=SESSION,
            sequence=0,
            sample_rate_hz=16_000,
            duration_ms=20,
            captured_at_ms=0,
            pcm=pcm[:-2],
        )
    with pytest.raises(ValidationError, match="unsupported sample rate"):
        AudioFrame(
            session_id=SESSION,
            sequence=0,
            sample_rate_hz=8_000,
            duration_ms=20,
            captured_at_ms=0,
            pcm=pcm[:320],
        )
    with pytest.raises(ValidationError):
        AudioFrame(
            session_id=SESSION,
            sequence=0,
            sample_rate_hz=16_000,
            channels=2,  # type: ignore[arg-type]
            duration_ms=20,
            captured_at_ms=0,
            pcm=pcm,
        )


def test_synthetic_pcm_rejects_out_of_range_amplitude() -> None:
    with pytest.raises(ValueError, match="amplitude"):
        square_wave_pcm(1.5, 10)
    assert peak_amplitude(b"") == 0.0


def test_tts_chunks_must_be_24_khz() -> None:
    pcm16 = square_wave_pcm(0.3, 320)
    frame16 = AudioFrame(
        session_id=SESSION,
        sequence=0,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=0,
        pcm=pcm16,
    )

    with pytest.raises(ValidationError, match="24 kHz"):
        TtsAudioChunk(stamp=_stamp(), segment_id=SEGMENT, frame=frame16)
    pcm24 = square_wave_pcm(0.3, TTS_SAMPLE_RATE_HZ // 50)
    frame24 = frame16.model_copy(update={"sample_rate_hz": 24_000, "pcm": pcm24})
    assert (
        TtsAudioChunk(stamp=_stamp(), segment_id=SEGMENT, frame=frame24).frame.sample_rate_hz
        == 24_000
    )


# --------------------------------------------------------- conversation ----


def test_conversation_request_forbids_empty_transcripts_and_tools() -> None:
    assert _request().tools == ()
    with pytest.raises(ValidationError, match="empty transcript"):
        _request(user_transcript="   ")
    with pytest.raises(ValidationError):
        _request(tools=({"type": "web_search"},))
    with pytest.raises(ValidationError):
        _request(max_output_tokens=251)
    with pytest.raises(ValidationError, match="turn and operation"):
        _request(stamp=_stamp(operation_id=None))


def test_conversation_request_rejects_browser_supplied_extras() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        _request(model="gpt-other")


def test_history_rejects_blank_text() -> None:
    with pytest.raises(ValidationError, match="blank"):
        HistoryMessage(role="assistant", text="  ", turn_id=TURN)  # type: ignore[arg-type]


# ------------------------------------------------------------ STT / TTS ----


def test_stt_config_is_phase0_and_keyterms_are_bounded() -> None:
    config = SttStreamConfig()
    assert config.expected_languages == ("hi", "en")
    assert config.keyterms == ()
    with pytest.raises(ValidationError):
        SttStreamConfig(translation=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SttStreamConfig(keyterms=tuple(f"term{i}" for i in range(51)))
    with pytest.raises(ValidationError, match="unique"):
        SttStreamConfig(keyterms=("NiaLabs", "NiaLabs"))
    with pytest.raises(ValidationError, match="markup"):
        SttStreamConfig(keyterms=("<b>x</b>",))
    with pytest.raises(ValidationError, match="trimmed"):
        SttStreamConfig(keyterms=(" padded",))


def test_stt_events_use_a_discriminated_union() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(SttEvent)
    event = SttTurnFinalized(stamp=_stamp(), transcript_id=SEGMENT, text="namaste")

    parsed = adapter.validate_json(event.model_dump_json())

    assert parsed == event
    assert not SttTurnFinalized(stamp=_stamp(), transcript_id=SEGMENT, text=" ").is_usable


def test_tts_voice_config_disables_cloning_and_storage() -> None:
    config = TtsVoiceConfig(provider="sarvam", model="bulbul:v3", voice_id="priya")

    assert config.sample_rate_hz == 24_000
    assert config.speaking_rate == Decimal("1.0")
    with pytest.raises(ValidationError):
        TtsVoiceConfig(provider="sarvam", model="bulbul:v3", voice_id="priya", voice_cloning=True)  # type: ignore[arg-type]


def test_tts_segment_is_capped_at_500_characters() -> None:
    def segment(text: str) -> TtsSegmentRequest:
        return TtsSegmentRequest(
            stamp=_stamp(),
            logical_request_id=OPERATION,
            segment_id=SEGMENT,
            sequence=0,
            text=text,
            language_code="hi-IN",  # type: ignore[arg-type]
        )

    assert segment("क" * 500).text
    with pytest.raises(ValidationError):
        segment("क" * 501)
    with pytest.raises(ValidationError, match="blank"):
        segment("   ")


# ------------------------------------------------------ speech/transport ---


def test_speech_event_timeline_is_ordered() -> None:
    with pytest.raises(ValidationError, match="timeline"):
        SpeechActivityEvent(
            kind=SpeechActivityKind.SPEECH_STOPPED,
            at_ms=10,
            speech_started_at_ms=20,
            last_speech_at_ms=15,
        )


def test_playback_progress_requires_a_position() -> None:
    identity = {"worker_generation": 1, "cancellation_generation": 0, "segment_id": SEGMENT}

    with pytest.raises(ValidationError, match="position"):
        PlaybackAck(ack=PlaybackAckKind.PROGRESS, identity=identity)  # type: ignore[arg-type]
    assert (
        PlaybackAck(ack=PlaybackAckKind.PROGRESS, identity=identity, position_ms=40).position_ms
        == 40
    )  # type: ignore[arg-type]


# ----------------------------------------------------------------- usage ---


def test_unavailable_usage_carries_no_quantities_and_is_never_zero() -> None:
    unavailable = UsageReport.unavailable()

    assert not unavailable.is_available
    assert unavailable.quantity_of(UsageUnit.OUTPUT_TOKENS) is None
    item = UsageItem(unit=UsageUnit.OUTPUT_TOKENS, quantity=Decimal(3), source=UsageSource.MEASURED)
    with pytest.raises(ValidationError, match="unavailable"):
        UsageReport(reporting_status=UsageReportingStatus.UNAVAILABLE, items=(item,))
    with pytest.raises(ValidationError, match="at least one"):
        UsageReport(reporting_status=UsageReportingStatus.MEASURED)
    with pytest.raises(ValidationError, match="unique"):
        UsageReport(reporting_status=UsageReportingStatus.MEASURED, items=(item, item))


@pytest.mark.parametrize("quantity", [Decimal(-1), Decimal("NaN"), Decimal("0.1234567890123")])
def test_usage_quantities_are_finite_non_negative_and_precise(quantity: Decimal) -> None:
    with pytest.raises(ValidationError):
        UsageItem(unit=UsageUnit.REQUESTS, quantity=quantity, source=UsageSource.MEASURED)


def test_estimated_source_must_be_flagged() -> None:
    with pytest.raises(ValidationError, match="estimated"):
        UsageItem(unit=UsageUnit.REQUESTS, quantity=Decimal(1), source=UsageSource.ESTIMATED)


def test_rate_card_rejects_duplicate_meters() -> None:
    rate = UnitRate(
        provider="p",
        usage_unit=UsageUnit.REQUESTS,
        billing_unit="request",
        unit_rate=Decimal(1),
        rate_unit_quantity=Decimal(1),
        currency=Currency.USD,
    )

    with pytest.raises(ValidationError, match="same meter"):
        RateCard(rate_card_id="x", effective_date=NOW.date(), rates=(rate, rate))


# --------------------------------------------------------------- failures --


def test_every_error_type_maps_to_one_category_and_overload_is_capacity() -> None:
    assert set(ERROR_CATEGORY_BY_TYPE) == set(ErrorType)
    assert ERROR_CATEGORY_BY_TYPE[ErrorType.REALTIME_OVERLOAD] is ErrorCategory.CAPACITY


def test_normalized_failure_is_bounded_and_wrappable() -> None:
    failure = NormalizedFailure(
        component=ErrorComponent.STT,
        provider="deepgram",
        error_type=ErrorType.RATE_LIMITED,
        safe_message="rate limited",
        retryable=True,
        status_code=429,
        session_id=SESSION,
        occurred_at=NOW,
    )

    error = NormalizedFailureError(failure)

    assert failure.category is ErrorCategory.RATE_LIMIT
    assert str(error) == "stt:rate_limited"
    assert error.failure is failure
    with pytest.raises(ValidationError):
        failure.model_copy(update={}).model_validate(
            {**failure.model_dump(), "safe_message": "x" * 1001}
        )


# --------------------------------------------------------------- policies --


def test_policy_defaults_match_docs_02_section_24() -> None:
    retry, turns, queues = RetryPolicy(), TurnHandlingPolicy(), QueueLimits()

    assert (retry.maximum_attempts, retry.initial_backoff_ms, retry.maximum_backoff_ms) == (
        3,
        250,
        2000,
    )
    assert (turns.minimum_interruption_ms, turns.endpoint_deadline_ms) == (250, 700)
    assert (turns.activation_threshold, turns.playback_activation_threshold) == (0.5, 0.7)
    assert (queues.inbound_audio_ms, queues.response_segments, queues.playback_audio_ms) == (
        2000,
        5,
        10_000,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint_deadline_ms": 1001},
        {"endpoint_deadline_ms": 600},
        {"minimum_interruption_ms": 200},
        {"preemptive_generation": True},
        {"playback_activation_threshold": 0.4},
    ],
)
def test_turn_policy_rejects_unapproved_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TurnHandlingPolicy(**overrides)


def test_retry_policy_rejects_inverted_backoff() -> None:
    with pytest.raises(ValidationError, match="initial backoff"):
        RetryPolicy(initial_backoff_ms=3000)
