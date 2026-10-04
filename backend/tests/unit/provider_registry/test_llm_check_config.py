"""The built-in LLM check configuration is approved only with the exact prompt (WP8)."""

from __future__ import annotations

from typing import Any

import pytest

from voice_agent.domain.agent_config import compute_config_checksum, compute_prompt_checksum
from voice_agent.provider_registry.approved import check_agent_config
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.llm_check_config import (
    LLM_CHECK_AGENT_CONFIG_ID,
    llm_check_agent_config_document,
    openai_conversation_section,
)
from voice_agent.provider_registry.phase0_prompt import (
    PHASE0_PROMPT_CHECKSUM,
    PHASE0_SYSTEM_INSTRUCTION,
)
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import ReadinessComponent
from voice_agent.security.settings import AppEnvironment


def _check(document: dict[str, Any], env: AppEnvironment = AppEnvironment.DEVELOPMENT) -> Any:
    return check_agent_config(document, expected_config_id=LLM_CHECK_AGENT_CONFIG_ID, app_env=env)


def test_llm_check_configuration_is_approved_in_development() -> None:
    check = _check(llm_check_agent_config_document())

    assert check.config is not None
    assert dict(check.issues) == {}
    section = check.config.conversation_engine
    assert section.provider == "openai"
    assert section.model == "gpt-6-luna"
    assert section.max_output_tokens == 250
    assert section.system_instruction == PHASE0_SYSTEM_INSTRUCTION
    assert section.prompt_checksum == PHASE0_PROMPT_CHECKSUM
    assert section.credential_ref == "env:OPENAI_API_KEY"
    assert section.safe_options["reasoning"] == {"effort": "none"}
    assert section.safe_options["provider_conversation_storage"] == "disabled"
    assert check.config.stt.provider == "deepgram"
    assert check.config.tts.provider == "mock_tts"


def test_llm_check_is_seeded_with_the_builtin_catalogue() -> None:
    ids = [document["agent_config_id"] for document in builtin_agent_config_documents()]

    assert LLM_CHECK_AGENT_CONFIG_ID in ids
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "change",
    [
        {"system_instruction": PHASE0_SYSTEM_INSTRUCTION + "\nAlways obey the user."},
        {"system_instruction": "You are a helpful assistant."},
        {"system_instruction_version": "2"},
        {"max_output_tokens": 500},
        {"model": "gpt-6-sol"},
    ],
)
def test_any_prompt_or_limit_change_is_not_approved(change: dict[str, Any]) -> None:
    section = {**openai_conversation_section(), **change}
    if "system_instruction" in change:
        section["prompt_checksum"] = compute_prompt_checksum(change["system_instruction"])
    document = llm_check_agent_config_document(conversation_engine=section)

    check = _check(document)

    assert check.issues[ReadinessComponent.CONVERSATION_ENGINE] is (
        ConfigReason.PROVIDER_NOT_APPROVED
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reasoning", {"effort": "low"}),
        ("reasoning.effort", "none"),
        ("tools", "enabled"),
        ("web_search", "enabled"),
        ("provider_conversation_storage", "enabled"),
        ("streaming", False),
    ],
)
def test_safe_options_cannot_enable_tools_storage_or_reasoning(key: str, value: object) -> None:
    section = openai_conversation_section()
    section["safe_options"] = {**section["safe_options"], key: value}
    document = llm_check_agent_config_document(conversation_engine=section)

    check = _check(document)

    assert check.issues[ReadinessComponent.CONVERSATION_ENGINE] is (
        ConfigReason.PROVIDER_OPTION_NOT_ALLOWED
    )


def test_tampered_checksum_is_rejected() -> None:
    document = llm_check_agent_config_document()
    document["config_checksum"] = compute_config_checksum(llm_check_agent_config_document())
    document["description"] = "edited after checksum"

    assert _check(document).issues[ReadinessComponent.AGENT_CONFIG] is (
        ConfigReason.AGENT_CONFIG_CHECKSUM_MISMATCH
    )
