"""Immutable versioned ``agent_config`` structure (docs/02 §5, §24; docs/12 §7)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.domain.agent_config import (
    AgentConfig,
    AgentConfigStatus,
    compute_config_checksum,
    compute_prompt_checksum,
)

CANARY = "sk-canary" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2"


def test_baseline_document_validates_with_defaults(baseline_document: Any) -> None:
    config = AgentConfig.model_validate(baseline_document())

    assert config.status is AgentConfigStatus.ACTIVE
    assert config.timeout_policy.maximum_session_ms == 1_800_000
    assert config.timeout_policy.llm_first_token_ms == 8000
    assert config.retry_policy.maximum_attempts == 3
    assert config.turn_handling.vad.playback_activation_threshold == 0.7
    assert config.verify_checksum()


def test_configuration_is_immutable(baseline_document: Any) -> None:
    config = AgentConfig.model_validate(baseline_document())

    with pytest.raises(ValidationError):
        config.version = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        config.stt.model = "other"  # type: ignore[misc]


def test_checksum_detects_drift(baseline_document: Any) -> None:
    config = AgentConfig.model_validate(baseline_document())
    drifted = config.model_copy(update={"cost_rate_card_version": "other_card"})

    assert not drifted.verify_checksum()


def test_checksum_ignores_lifecycle_fields(baseline_document: Any) -> None:
    document = baseline_document()
    retired = {
        **document,
        "status": "retired",
        "retired_at": "2026-10-01T00:00:00Z",
        "retired_by": "maintenance",
        "revision": 2,
        "updated_at": "2026-10-01T00:00:00Z",
    }

    assert compute_config_checksum(retired) == document["config_checksum"]


def test_checksum_is_canonical_sha256(baseline_document: Any) -> None:
    checksum = baseline_document()["config_checksum"]

    assert checksum.startswith("sha256:")
    assert len(checksum) == len("sha256:") + 64


def test_prompt_checksum_normalizes_line_endings() -> None:
    assert compute_prompt_checksum("a\r\nb\rc") == compute_prompt_checksum("a\nb\nc")
    assert compute_prompt_checksum("a") != compute_prompt_checksum("b")


def test_prompt_checksum_mismatch_is_rejected(baseline_document: Any) -> None:
    document = baseline_document()
    document["conversation_engine"] = {
        **document["conversation_engine"],
        "system_instruction": "Changed instruction.",
    }

    with pytest.raises(ValidationError, match="prompt checksum"):
        AgentConfig.model_validate(document)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("stt", "api_key"),
        ("tts", "authorization"),
        ("conversation_engine", "endpoint_url"),
        ("transport", "api_secret"),
    ],
)
def test_unknown_section_fields_are_rejected(
    baseline_document: Any, section: str, field: str
) -> None:
    document = baseline_document()
    document[section] = {**document[section], field: CANARY}

    with pytest.raises(ValidationError) as caught:
        AgentConfig.model_validate(document)

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)
    # ``errors()`` includes raw input by default; safe mappers must opt out.
    assert CANARY not in repr(caught.value.errors(include_input=False))


def test_unknown_root_field_is_rejected(baseline_document: Any) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(baseline_document(mongodb_uri=CANARY))


@pytest.mark.parametrize(
    "credential_ref",
    ["OPENAI_API_KEY", "file:C:/secrets.env", "env:../x", "env:openai_api_key", "", CANARY],
)
def test_credential_ref_shape_is_validated(baseline_document: Any, credential_ref: str) -> None:
    document = baseline_document()
    document["stt"] = {**document["stt"], "credential_ref": credential_ref}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(document)


def test_safe_options_are_bounded(baseline_document: Any) -> None:
    document = baseline_document()
    document["stt"] = {**document["stt"], "safe_options": {f"k{i}": True for i in range(51)}}

    with pytest.raises(ValidationError, match="50"):
        AgentConfig.model_validate(document)


def test_safe_options_payload_size_is_bounded(baseline_document: Any) -> None:
    document = baseline_document()
    document["tts"] = {**document["tts"], "safe_options": {"big": "x" * 17_000}}

    with pytest.raises(ValidationError, match="16 KiB"):
        AgentConfig.model_validate(document)


@pytest.mark.parametrize(
    "turn_handling",
    [
        {"maximum_endpointing_ms": 1001},
        {"minimum_endpointing_ms": 600},
        {"minimum_endpointing_ms": 900, "maximum_endpointing_ms": 800},
        {"minimum_interruption_ms": 200},
        {"preemptive_generation": True},
        {"vad": {"playback_activation_threshold": 0.4}},
    ],
)
def test_turn_handling_bounds(baseline_document: Any, turn_handling: dict[str, Any]) -> None:
    document = baseline_document()
    document["turn_handling"] = {**document["turn_handling"], **turn_handling}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(document)


@pytest.mark.parametrize(
    "timeouts",
    [{"maximum_session_ms": 1_800_001}, {"maximum_user_turn_ms": 120_001}, {"llm_total_ms": 0}],
)
def test_timeout_bounds(baseline_document: Any, timeouts: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(baseline_document(timeout_policy=timeouts))


def test_strict_scalars_reject_coercion(baseline_document: Any) -> None:
    document = baseline_document()
    document["stt"] = {**document["stt"], "sample_rate_hz": "16000"}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(document)
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(baseline_document(version="1"))


def test_version_above_one_requires_change_note(baseline_document: Any) -> None:
    with pytest.raises(ValidationError, match="change_note"):
        AgentConfig.model_validate(baseline_document(version=2))

    config = AgentConfig.model_validate(baseline_document(version=2, change_note="tuned"))
    assert config.version == 2


def test_lifecycle_consistency(baseline_document: Any) -> None:
    with pytest.raises(ValidationError, match="retired"):
        AgentConfig.model_validate(baseline_document(status="retired"))
    with pytest.raises(ValidationError, match="expires_at"):
        AgentConfig.model_validate(baseline_document(expires_at="2026-11-01T00:00:00Z"))


def test_system_instruction_never_appears_in_validation_errors(baseline_document: Any) -> None:
    document = baseline_document()
    document["conversation_engine"] = {
        **document["conversation_engine"],
        "system_instruction": CANARY,
    }

    with pytest.raises(ValidationError) as caught:
        AgentConfig.model_validate(document)

    assert CANARY not in str(caught.value)


def test_maps_to_runtime_turn_handling_policy(baseline_document: Any) -> None:
    config = AgentConfig.model_validate(baseline_document())

    policy = config.turn_handling_policy()

    assert isinstance(policy, TurnHandlingPolicy)
    assert policy.playback_activation_threshold == 0.7
    assert policy.endpoint_deadline_ms == 700
    assert policy.stt_finalize_timeout_ms == config.timeout_policy.stt_finalize_ms


def test_tags_must_be_unique(baseline_document: Any) -> None:
    with pytest.raises(ValidationError, match="unique"):
        AgentConfig.model_validate(
            baseline_document(tags=("a", "a"), config_checksum="sha256:" + "0" * 64)
        )


def test_checksum_of_model_equals_checksum_of_document(baseline_document: Any) -> None:
    document = baseline_document()

    assert (
        compute_config_checksum(AgentConfig.model_validate(document))
        == (document["config_checksum"])
    )
