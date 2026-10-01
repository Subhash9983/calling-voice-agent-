"""Normalized STT configuration -> Deepgram live-listen query options (docs/07 §5-§6).

The browser never selects any of these; they come only from the immutable,
server-approved agent configuration. The Phase 0 live keyterm list is empty
(Decision 043): a non-empty list maps to ``keyterm`` prompting only when the
paid feature is separately approved and the model supports it; otherwise it
is rejected explicitly (never silently dropped).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from voice_agent.contracts.audio import VAD_SAMPLE_RATE_HZ
from voice_agent.contracts.stt import SttStreamConfig

DEEPGRAM_PROVIDER: Final = "deepgram"
DEEPGRAM_MODEL: Final = "nova-3"
DEEPGRAM_ADAPTER_VERSION: Final = "deepgram-stt-0.1.0"
MULTILINGUAL_LANGUAGE: Final = "multi"
AUDIO_ENCODING: Final = "linear16"
# Keyterm prompting is a Nova-3 feature (docs/07 §6); older models use keywords.
KEYTERM_MODELS: Final = frozenset({DEEPGRAM_MODEL})
# User decision 2026-10-01 (docs/15): every stream opts out of Deepgram's
# Model Improvement Program so session audio is never used for training.
MIP_OPT_OUT: Final = True
_EXPECTED_LANGUAGES: Final = frozenset({"hi", "en"})

QueryValue = str | tuple[str, ...]


class UnsupportedSttConfigError(ValueError):
    """The normalized configuration has no approved Deepgram mapping."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeytermRejectedError(UnsupportedSttConfigError):
    """Keyterms were configured but cannot be enabled."""


@dataclass(frozen=True, slots=True)
class DeepgramStreamOptions:
    params: Mapping[str, QueryValue]
    keyterm_count: int = 0


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _language(config: SttStreamConfig) -> str:
    if config.sample_rate_hz != VAD_SAMPLE_RATE_HZ:
        raise UnsupportedSttConfigError("sample_rate_not_supported")
    if set(config.expected_languages) != _EXPECTED_LANGUAGES or not config.code_switching:
        raise UnsupportedSttConfigError("language_mode_not_supported")
    return MULTILINGUAL_LANGUAGE


def _keyterms(config: SttStreamConfig, model: str, approved: bool) -> tuple[str, ...]:
    if not config.keyterms:
        return ()
    if not approved:
        raise KeytermRejectedError("keyterms_not_approved")
    if model not in KEYTERM_MODELS:
        raise KeytermRejectedError("keyterms_unsupported_by_model")
    return config.keyterms


def build_stream_options(
    config: SttStreamConfig, *, model: str, keyterms_approved: bool = False
) -> DeepgramStreamOptions:
    params: dict[str, QueryValue] = {
        "model": model,
        "language": _language(config),
        "encoding": AUDIO_ENCODING,
        "sample_rate": str(config.sample_rate_hz),
        "channels": "1",
        "interim_results": _flag(config.partial_transcripts),
        "punctuate": _flag(config.punctuation),
        "smart_format": _flag(config.smart_formatting),
        "mip_opt_out": _flag(MIP_OPT_OUT),
    }
    keyterms = _keyterms(config, model, keyterms_approved)
    if keyterms:
        params["keyterm"] = keyterms
    return DeepgramStreamOptions(params=MappingProxyType(params), keyterm_count=len(keyterms))
