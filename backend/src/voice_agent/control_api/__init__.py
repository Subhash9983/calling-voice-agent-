"""FastAPI control-plane entry point (sessions, join tokens, diagnostics, health).

WP4: application factory (``control_api.app.create_app``), loopback-only
server entry point, request correlation, safe structured logging, and the
approved docs/04 §23 routes backed by in-memory stores and a mock transport.
It never runs realtime STT, conversation, or TTS work (docs/03 §5).
"""
