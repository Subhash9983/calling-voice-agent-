"""Suite-wide gating for tests that touch real R&D services.

``atlas``-marked tests (the R&D MongoDB Atlas database) and ``livekit``-marked
tests (LiveKit Cloud, metered) are skipped unless their marker is selected
explicitly with ``-m`` *and* ``VOICE_AGENT_SECRETS_FILE`` is present in the
process environment, so a default ``pytest`` run is always offline.
"""

from __future__ import annotations

import os

import pytest

SECRETS_FILE_VARIABLE = "VOICE_AGENT_SECRETS_FILE"
REAL_SERVICE_MARKERS = ("atlas", "livekit")
# pymongo 4.18.1's background server monitor can leave a connecting socket for
# the GC when a client closes mid-connect on Windows (traced to
# ``pymongo/asynchronous/monitor.py`` -> ``pool.py``). That is driver-internal,
# so only socket ResourceWarnings are ignored, and only for real-Atlas tests.
ATLAS_SOCKET_WARNING_FILTERS = (
    "ignore:unclosed <socket.socket:ResourceWarning",
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning",
)


def _selected(config: pytest.Config, marker: str) -> bool:
    expression = str(config.getoption("markexpr") or "")
    return marker in expression and f"not {marker}" not in expression


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    configured = bool(os.environ.get(SECRETS_FILE_VARIABLE))
    for item in items:
        if item.get_closest_marker("atlas") is not None:
            for spec in ATLAS_SOCKET_WARNING_FILTERS:
                item.add_marker(pytest.mark.filterwarnings(spec))
        for marker in REAL_SERVICE_MARKERS:
            # A real marker only: ``item.keywords`` also holds path parts such
            # as the ``livekit`` test package name.
            if item.get_closest_marker(marker) is None:
                continue
            if not _selected(config, marker):
                item.add_marker(
                    pytest.mark.skip(reason=f"{marker} tests run only with -m {marker}")
                )
            elif not configured:
                item.add_marker(pytest.mark.skip(reason=f"{SECRETS_FILE_VARIABLE} is not set"))
