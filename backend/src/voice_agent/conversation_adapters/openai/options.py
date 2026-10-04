"""Approved GPT-6 Luna request mapping (docs/08 §5, §15; docs/10 §7; docs/15 §2.4).

Request construction order (docs/10 §7): the exact canonical system
instruction (Responses ``instructions``), then the bounded normalized history
of accepted user transcripts and delivered assistant text, then the current
accepted final transcript. Nothing else is added: no tools, search, files,
provider memory, previous-response chaining, metadata, or browser input.

- ``reasoning = {"effort": "none"}`` (latency-sensitive voice path);
- ``store = false`` (``provider_conversation_storage: disabled``);
- ``stream = true`` (Responses HTTP SSE);
- ``max_output_tokens`` from the immutable configuration (250);
- provider-default sampling (no temperature/top_p sent);
- ``service_tier = "default"``: Standard processing, no Fast/priority uplift.
"""

from __future__ import annotations

from typing import Any, Final

from voice_agent.contracts.conversation import ConversationRequest

OPENAI_PROVIDER: Final = "openai"
OPENAI_MODEL: Final = "gpt-6-luna"
OPENAI_ADAPTER_VERSION: Final = "openai-responses-0.1.0"
REASONING_EFFORT: Final = "none"
SERVICE_TIER: Final = "default"
CURRENT_USER_ROLE: Final = "user"


def request_params(request: ConversationRequest, *, model: str = OPENAI_MODEL) -> dict[str, Any]:
    """The complete Responses request body for one generation attempt."""
    if request.tools:  # pragma: no cover - the contract type only admits ``()``
        raise ValueError("Phase 0 tools are always disabled")
    history = [{"role": message.role.value, "content": message.text} for message in request.history]
    current = {"role": CURRENT_USER_ROLE, "content": request.user_transcript}
    return {
        "model": model,
        "instructions": request.system_instruction,
        "input": [*history, current],
        "max_output_tokens": request.max_output_tokens,
        "reasoning": {"effort": REASONING_EFFORT},
        "store": False,
        "stream": True,
        "service_tier": SERVICE_TIER,
    }
