"""Deepgram Nova-3 multilingual streaming STT adapter (docs/07, docs/13 §7).

Only :mod:`voice_agent.stt_adapters.deepgram.sdk_binding` imports the
official ``deepgram-sdk``; every other module works on plain mappings and
internal contracts so the adapter is testable offline.
"""
