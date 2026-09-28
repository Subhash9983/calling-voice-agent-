"""Browser override guard (docs/01 §18-§19, docs/04 §6, docs/12 §11, §19).

The browser may submit only the approved session-creation fields, and the
fixed-value fields only with their single Phase 0 value. It can never select
a provider, model, prompt, voice, endpoint, credential, or provider option.
Rejections name only approved or known-restricted fields; any other
browser-supplied key is reported as ``<unrecognized>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from voice_agent.security.config_errors import (
    UNRECOGNIZED_SETTING,
    ConfigDiagnostic,
    ConfigReason,
)

BROWSER_SESSION_CREATE_FIELDS: frozenset[str] = frozenset(
    {"client_request_id", "agent_config_id", "channel", "session_mode", "language_mode"}
)
BROWSER_FIXED_VALUES: Mapping[str, str] = MappingProxyType(
    {"channel": "browser", "session_mode": "interactive_test", "language_mode": "auto"}
)
RESTRICTED_BROWSER_FIELDS: frozenset[str] = frozenset(
    {
        "provider",
        "model",
        "voice",
        "voice_id",
        "prompt",
        "prompt_id",
        "system_instruction",
        "endpoint",
        "url",
        "server_url",
        "credential_ref",
        "api_key",
        "safe_options",
        "provider_options",
        "temperature",
        "max_output_tokens",
        "reasoning",
        "tools",
        "speaking_rate",
        "keyterms",
        "stt",
        "tts",
        "conversation_engine",
        "transport",
        "turn_handling",
        "timeout_policy",
        "retry_policy",
    }
)


class BrowserOverrideError(Exception):
    def __init__(self, diagnostics: tuple[ConfigDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        fields = ", ".join(item.setting or UNRECOGNIZED_SETTING for item in diagnostics)
        super().__init__(f"{ConfigReason.BROWSER_OVERRIDE_REJECTED.value}: {fields}")


def _label(key: str, allowed: frozenset[str]) -> str:
    return key if key in allowed or key in RESTRICTED_BROWSER_FIELDS else UNRECOGNIZED_SETTING


def rejected_browser_fields(
    payload: Mapping[str, object],
    allowed: frozenset[str] = BROWSER_SESSION_CREATE_FIELDS,
) -> tuple[ConfigDiagnostic, ...]:
    """Return one diagnostic per disallowed key or fixed-value mismatch."""
    rejected: list[ConfigDiagnostic] = []
    for key, value in payload.items():
        name = str(key)
        fixed = BROWSER_FIXED_VALUES.get(name)
        if name not in allowed or (fixed is not None and value != fixed):
            rejected.append(
                ConfigDiagnostic(
                    ConfigReason.BROWSER_OVERRIDE_REJECTED, setting=_label(name, allowed)
                )
            )
    return tuple(rejected)


def ensure_browser_payload_allowed(
    payload: Mapping[str, object],
    allowed: frozenset[str] = BROWSER_SESSION_CREATE_FIELDS,
) -> None:
    rejected = rejected_browser_fields(payload, allowed)
    if rejected:
        raise BrowserOverrideError(rejected)
