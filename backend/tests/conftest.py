"""Suite-wide gating for tests that touch the real R&D Atlas database.

``atlas``-marked tests are skipped unless they are selected explicitly with
``-m atlas`` *and* ``VOICE_AGENT_SECRETS_FILE`` is present in the process
environment, so a default ``pytest`` run is always offline.
"""

from __future__ import annotations

import os

import pytest

SECRETS_FILE_VARIABLE = "VOICE_AGENT_SECRETS_FILE"


def _atlas_selected(config: pytest.Config) -> bool:
    expression = str(config.getoption("markexpr") or "")
    return "atlas" in expression and "not atlas" not in expression


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selected = _atlas_selected(config)
    configured = bool(os.environ.get(SECRETS_FILE_VARIABLE))
    for item in items:
        if "atlas" not in item.keywords:
            continue
        if not selected:
            item.add_marker(pytest.mark.skip(reason="Atlas tests run only with -m atlas"))
        elif not configured:
            item.add_marker(pytest.mark.skip(reason=f"{SECRETS_FILE_VARIABLE} is not set"))
