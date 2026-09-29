"""LiveKit transport adapters (docs/06 §2).

- ``control``: control-plane adapter (``livekit-api``) used by the control API
  for room/dispatch/token/inspection/cleanup;
- ``session``: worker session-transport adapter (``livekit-agents``/``rtc``).

LiveKit SDK types never cross either adapter's port.
"""
