"""Browser override guard (docs/01 §19, docs/04 §6, docs/12 §11)."""

from __future__ import annotations

import pytest

from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.overrides import (
    BrowserOverrideError,
    ensure_browser_payload_allowed,
    rejected_browser_fields,
)

VALID = {
    "client_request_id": "44444444-4444-4444-8444-444444444444",
    "agent_config_id": "11111111-1111-4111-8111-111111111111",
    "channel": "browser",
    "session_mode": "interactive_test",
    "language_mode": "auto",
}
CANARY = "sk-canary" + "Bb1Rr2Oo3Ww4Ss5Ee6"


def test_approved_session_payload_is_accepted() -> None:
    assert rejected_browser_fields(VALID) == ()
    ensure_browser_payload_allowed(VALID)


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "model",
        "voice_id",
        "system_instruction",
        "endpoint",
        "credential_ref",
        "safe_options",
    ],
)
def test_restricted_fields_are_rejected_by_name(field: str) -> None:
    (diagnostic,) = rejected_browser_fields({**VALID, field: "override"})

    assert diagnostic.reason is ConfigReason.BROWSER_OVERRIDE_REJECTED
    assert diagnostic.setting == field


@pytest.mark.parametrize(
    ("field", "value"),
    [("channel", "telephony"), ("session_mode", "benchmark"), ("language_mode", "hi")],
)
def test_fixed_value_fields_accept_only_the_phase0_value(field: str, value: str) -> None:
    (diagnostic,) = rejected_browser_fields({**VALID, field: value})

    assert diagnostic.setting == field


def test_unknown_browser_key_is_not_echoed() -> None:
    with pytest.raises(BrowserOverrideError) as caught:
        ensure_browser_payload_allowed({**VALID, CANARY: "x"})

    assert CANARY not in str(caught.value)
    assert caught.value.diagnostics[0].setting == "<unrecognized>"
