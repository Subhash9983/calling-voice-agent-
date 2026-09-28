"""Control-plane application services used by route handlers (docs/03 §5).

Services orchestrate domain rules over repository and transport ports. They
never perform realtime STT, conversation, or TTS work and never wait for a
realtime response.
"""
