"""Credential references, value-free classification, and late resolution (docs/12 §5, §9).

``credential_ref`` values use only the ``env:<NAME>`` scheme and only the
provider-adapter allowlist. ``MONGODB_URI`` and the LiveKit key pair are
bootstrap-only: loaded at process start, never selectable by reference.
Classification reports available/missing/blank/placeholder/malformed and
never a value, prefix, suffix, or length.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import SecretStr

from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.settings import BootstrapSettings

CREDENTIAL_REF_PREFIX = "env:"
RESOLVABLE_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "SARVAM_API_KEY"}
)
# Allowlisted only once their challenger adapters are approved (docs/12 §9).
CHALLENGER_CREDENTIAL_NAMES: frozenset[str] = frozenset({"XAI_API_KEY", "ELEVENLABS_API_KEY"})
BOOTSTRAP_ONLY_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {"MONGODB_URI", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"}
)
# Credential required by each real provider adapter; mock adapters need none.
PROVIDER_CREDENTIAL_NAME: Mapping[str, str] = MappingProxyType(
    {
        "deepgram": "DEEPGRAM_API_KEY",
        "openai": "OPENAI_API_KEY",
        "sarvam": "SARVAM_API_KEY",
        "xai": "XAI_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
    }
)
LIVEKIT_CREDENTIAL_NAMES: tuple[str, str] = ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")

MIN_API_KEY_LENGTH = 8
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "changeme",
    "change-me",
    "change_me",
    "replace_me",
    "replace-me",
    "replaceme",
    "your-",
    "your_",
    "placeholder",
    "example",
)
_ANGLE_PLACEHOLDER = re.compile(r"<[^<>]*>")
_REPEATED_X = re.compile(r"x{8,}", re.IGNORECASE)
_PRINTABLE_TOKEN = re.compile(r"^[\x21-\x7e]+$")
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.?)+$|^\[[0-9A-Fa-f:.]+\]$")
_PORT = re.compile(r"^[0-9]{1,5}$")
_MONGODB_SCHEMES: tuple[str, str] = ("mongodb://", "mongodb+srv://")


class CredentialStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    BLANK = "blank"
    PLACEHOLDER = "placeholder"
    MALFORMED = "malformed"

    @property
    def reason(self) -> ConfigReason:
        return _STATUS_REASON[self]


_STATUS_REASON: Mapping[CredentialStatus, ConfigReason] = MappingProxyType(
    {
        CredentialStatus.AVAILABLE: ConfigReason.OK,
        CredentialStatus.MISSING: ConfigReason.CREDENTIAL_MISSING,
        CredentialStatus.BLANK: ConfigReason.CREDENTIAL_BLANK,
        CredentialStatus.PLACEHOLDER: ConfigReason.CREDENTIAL_PLACEHOLDER,
        CredentialStatus.MALFORMED: ConfigReason.CREDENTIAL_MALFORMED,
    }
)


class CredentialError(Exception):
    """Reference/resolution failure; the message is the reason code only."""

    def __init__(self, reason: ConfigReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def parse_credential_ref(ref: str) -> str:
    """Return the allowlisted credential name, or raise ``CredentialError``."""
    name = ref.removeprefix(CREDENTIAL_REF_PREFIX) if ref.startswith(CREDENTIAL_REF_PREFIX) else ""
    if name not in RESOLVABLE_CREDENTIAL_NAMES:
        raise CredentialError(ConfigReason.CREDENTIAL_REF_NOT_ALLOWED)
    return name


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        bool(_ANGLE_PLACEHOLDER.search(value))
        or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or bool(_REPEATED_X.search(value))
        or len(set(value)) == 1
    )


def _split_host_port(entry: str) -> tuple[str, str | None]:
    if entry.startswith("["):
        address, _, rest = entry.partition("]")
        if not rest:
            return address + "]", None
        return (address + "]", rest[1:]) if rest.startswith(":") else ("", None)
    host, separator, port = entry.partition(":")
    return host, port if separator else None


def _valid_host(entry: str, *, allow_port: bool) -> bool:
    host, port = _split_host_port(entry)
    if port is not None and (not allow_port or not _PORT.match(port)):
        return False
    return bool(_HOST.match(host))


def _valid_mongodb_uri(value: str) -> bool:
    scheme = next((s for s in _MONGODB_SCHEMES if value.startswith(s)), None)
    if scheme is None or not _PRINTABLE_TOKEN.match(value):
        return False
    remainder = value[len(scheme) :]
    authority = re.split(r"[/?]", remainder, maxsplit=1)[0]
    hosts = authority.rpartition("@")[2]
    entries = hosts.split(",") if hosts else []
    if scheme == "mongodb+srv://":
        return len(entries) == 1 and _valid_host(entries[0], allow_port=False)
    return bool(entries) and all(_valid_host(entry, allow_port=True) for entry in entries)


def classify_credential(name: str, secret: SecretStr | None) -> CredentialStatus:
    """Classify a configured credential without exposing any part of it."""
    if secret is None:
        return CredentialStatus.MISSING
    value = secret.get_secret_value()
    if not value.strip():
        return CredentialStatus.BLANK
    if _is_placeholder(value):
        return CredentialStatus.PLACEHOLDER
    if name == "MONGODB_URI":
        return (
            CredentialStatus.AVAILABLE if _valid_mongodb_uri(value) else CredentialStatus.MALFORMED
        )
    if len(value) < MIN_API_KEY_LENGTH or not _PRINTABLE_TOKEN.match(value):
        return CredentialStatus.MALFORMED
    return CredentialStatus.AVAILABLE


class CredentialResolver:
    """Server-side resolver used only inside adapter construction (docs/12 §9, §10)."""

    def __init__(self, settings: BootstrapSettings) -> None:
        self._settings = settings

    def __repr__(self) -> str:
        return "CredentialResolver()"

    def resolve(self, ref: str) -> SecretStr:
        name = parse_credential_ref(ref)
        secret = self._settings.secret(name)
        status = classify_credential(name, secret)
        if status is not CredentialStatus.AVAILABLE or secret is None:
            raise CredentialError(status.reason)
        return secret
