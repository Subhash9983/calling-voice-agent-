"""Defence-in-depth redaction for logs, events, and errors (docs/12 §13).

Redacted values are replaced by one constant marker. No prefix, suffix,
length, hash, or reversible encoding of a secret is ever emitted. Code must
still avoid putting secrets into logs in the first place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

# Baseline and challenger secret names (docs/12 §5).
SECRET_SETTING_NAMES: frozenset[str] = frozenset(
    {
        "MONGODB_URI",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY",
        "SARVAM_API_KEY",
        "XAI_API_KEY",
        "ELEVENLABS_API_KEY",
    }
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|authorization|cookie|credential"
    r"|private[_-]?key|access[_-]?key|signature|mongodb[_-]?uri|connection[_-]?string)",
    re.IGNORECASE,
)
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_MONGODB_URI = re.compile(r"(mongodb(?:\+srv)?://)([^@\s/]+)@([^\s?/]+)([^\s?]*)(\?\S*)?")
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SIGNED_QUERY = re.compile(
    r"([?&](?:x-amz-signature|x-amz-credential|x-goog-signature|signature|sig|token"
    r"|access_token|api_key|key)=)[^&\s#]+",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"\b([A-Za-z0-9_]*(?:api[_-]?key|secret|password|token)[A-Za-z0-9_]*)"
    r"(\s*[:=]\s*)(['\"]?)[^\s'\",;]+\3",
    re.IGNORECASE,
)
_PROVIDER_KEY = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")
_HIGH_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
_MIN_CHARACTER_CLASSES = 2


def _looks_high_entropy(token: str) -> bool:
    if _CANONICAL_UUID.match(token):
        return False
    classes = sum(
        (
            any(ch.islower() for ch in token),
            any(ch.isupper() for ch in token),
            any(ch.isdigit() for ch in token),
        )
    )
    return classes >= _MIN_CHARACTER_CLASSES


def _redact_high_entropy(match: re.Match[str]) -> str:
    token = match.group(0)
    return REDACTED if _looks_high_entropy(token) else token


def _redact_mongodb_uri(match: re.Match[str]) -> str:
    scheme, _userinfo, host, path, query = match.groups()
    redacted_query = f"?{REDACTED}" if query else ""
    return f"{scheme}{REDACTED}@{host}{path}{redacted_query}"


def redact_text(text: str) -> str:
    """Remove credential-like material from free text."""
    result = _MONGODB_URI.sub(_redact_mongodb_uri, text)
    result = _JWT.sub(REDACTED, result)
    result = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", result)
    result = _SIGNED_QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    result = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    result = _PROVIDER_KEY.sub(REDACTED, result)
    return _HIGH_ENTROPY_CANDIDATE.sub(_redact_high_entropy, result)


def is_sensitive_key(key: str) -> bool:
    return key.upper() in SECRET_SETTING_NAMES or bool(_SENSITIVE_KEY_PATTERN.search(key))


def redact_value(value: Any) -> Any:
    """Recursively redact mappings, sequences, and strings; returns new objects."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes | bytearray):
        return REDACTED
    if isinstance(value, Sequence):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_value(data)
    if not isinstance(redacted, dict):  # pragma: no cover - Mapping always yields dict
        raise TypeError("mapping redaction must return a dict")
    return redacted


def safe_credential_evidence(name: str, *, available: bool) -> dict[str, str | bool]:
    """The only permitted credential evidence shape (docs/12 §13)."""
    if name not in SECRET_SETTING_NAMES:
        raise ValueError("unknown credential name")
    return {
        "credential_name": name,
        "credential_source": "external_secret_file",
        "credential_available": available,
    }
