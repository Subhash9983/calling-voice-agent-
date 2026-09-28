"""``python -m voice_agent.control_api`` runs the local control API."""

from __future__ import annotations

from voice_agent.control_api.server import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
