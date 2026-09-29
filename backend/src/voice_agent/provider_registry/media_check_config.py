"""Built-in LiveKit media-check configuration (development only; docs/14 §12 exit gate).

Real LiveKit transport with the offline mock STT/conversation/TTS sections,
so browser microphone audio can reach the worker and worker test audio can
play in the browser without any STT, LLM, or TTS credential. The worker's
media-check mode (test tone or echo) never calls a paid AI provider. Seeded
into MongoDB by ``maintenance.database seed-configs``; point
``APP_DEFAULT_AGENT_CONFIG_ID`` at :data:`MEDIA_CHECK_AGENT_CONFIG_ID`.
"""

from __future__ import annotations

from typing import Any, Final

from voice_agent.domain.agent_config import compute_config_checksum
from voice_agent.provider_registry.approved import LIVEKIT_RECORDED_CREDENTIAL_REF
from voice_agent.provider_registry.mock_config import mock_agent_config_document

MEDIA_CHECK_AGENT_CONFIG_ID: Final = "00000000-0000-4000-8000-00000000c6a1"
MEDIA_CHECK_AGENT_ID: Final = "00000000-0000-4000-8000-00000000a6a1"
LIVEKIT_SESSION_ADAPTER_VERSION: Final = "livekit-session-0.1.0"
_CREATED_AT: Final = "2026-09-29T00:00:00Z"


def media_check_agent_config_document(**overrides: Any) -> dict[str, Any]:
    """A fresh media-check configuration document with a correct checksum."""
    fields: dict[str, Any] = {
        "agent_config_id": MEDIA_CHECK_AGENT_CONFIG_ID,
        "agent_id": MEDIA_CHECK_AGENT_ID,
        "name": "LiveKit media check (no AI providers)",
        "description": "Two-way LiveKit audio check: test tone/echo; no STT, LLM, or TTS.",
        "transport": {
            "provider": "livekit",
            "adapter_version": LIVEKIT_SESSION_ADAPTER_VERSION,
            "credential_ref": LIVEKIT_RECORDED_CREDENTIAL_REF,
            "safe_options": {},
        },
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        **overrides,
    }
    document = mock_agent_config_document(**fields)
    document["config_checksum"] = compute_config_checksum(document)
    return document
