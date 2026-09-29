"""``python -m voice_agent.agent_worker`` runs the local LiveKit agent worker."""

from __future__ import annotations

from voice_agent.agent_worker.server import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
