"""Built-in LLM check configuration (development only; docs/14 §14 WP8).

Real LiveKit transport, the approved Deepgram Nova-3 STT section, and the
approved OpenAI GPT-6 Luna conversation section carrying the exact
``phase0_general_voice_assistant_v1`` instruction and checksum (docs/10 §2,
§9), with the offline mock TTS section: an accepted durable final transcript
authorizes exactly one GPT-6 Luna generation, and the delivered segments are
published to the browser as ``va.response.v1`` text. No TTS provider is
called. Seeded into MongoDB by ``maintenance.database seed-configs``; point
``APP_DEFAULT_AGENT_CONFIG_ID`` at :data:`LLM_CHECK_AGENT_CONFIG_ID` and run the
worker with ``--media-mode llm`` (live use needs the user's key and a docs/15
budget first).
"""

from __future__ import annotations

from typing import Any, Final

from voice_agent.conversation_adapters.openai.options import (
    OPENAI_ADAPTER_VERSION,
    OPENAI_MODEL,
    OPENAI_PROVIDER,
)
from voice_agent.domain.agent_config import compute_config_checksum
from voice_agent.provider_registry.phase0_prompt import (
    PHASE0_PROMPT_CHECKSUM,
    PHASE0_PROMPT_ID,
    PHASE0_PROMPT_VERSION,
    PHASE0_SYSTEM_INSTRUCTION,
)
from voice_agent.provider_registry.stt_check_config import stt_check_agent_config_document

LLM_CHECK_AGENT_CONFIG_ID: Final = "00000000-0000-4000-8000-00000000c8a1"
LLM_CHECK_AGENT_ID: Final = "00000000-0000-4000-8000-00000000a8a1"
OPENAI_CREDENTIAL_REF: Final = "env:OPENAI_API_KEY"
PHASE0_TOOL_SET_VERSION: Final = "phase0_empty_v1"
_CREATED_AT: Final = "2026-10-01T00:00:00Z"


def openai_conversation_section() -> dict[str, Any]:
    """The approved Phase 0 GPT-6 Luna section (docs/08 §5, §19; docs/10 §9)."""
    return {
        "provider": OPENAI_PROVIDER,
        "model": OPENAI_MODEL,
        "adapter_version": OPENAI_ADAPTER_VERSION,
        "credential_ref": OPENAI_CREDENTIAL_REF,
        "max_output_tokens": 250,
        "prompt_id": PHASE0_PROMPT_ID,
        "system_instruction": PHASE0_SYSTEM_INSTRUCTION,
        "system_instruction_version": PHASE0_PROMPT_VERSION,
        "prompt_checksum": PHASE0_PROMPT_CHECKSUM,
        "tool_set_version": PHASE0_TOOL_SET_VERSION,
        "safe_options": {
            "reasoning": {"effort": "none"},
            "streaming": True,
            "tools": "disabled",
            "web_search": "disabled",
            "provider_conversation_storage": "disabled",
        },
    }


def llm_check_agent_config_document(**overrides: Any) -> dict[str, Any]:
    """A fresh LLM-check configuration document with a correct checksum."""
    fields: dict[str, Any] = {
        "agent_config_id": LLM_CHECK_AGENT_CONFIG_ID,
        "agent_id": LLM_CHECK_AGENT_ID,
        "name": "GPT-6 Luna LLM check (no TTS)",
        "description": "Deepgram transcripts -> GPT-6 Luna text responses in the browser; no TTS.",
        "conversation_engine": openai_conversation_section(),
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        **overrides,
    }
    document = stt_check_agent_config_document(**fields)
    document["config_checksum"] = compute_config_checksum(document)
    return document
