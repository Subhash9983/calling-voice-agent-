"""Normalized, value-free configuration diagnostics (docs/12 §3, §12, §13).

A diagnostic carries only a normalized reason code, a known setting name, the
source class, a severity, and (for the secret file) a line number. It never
carries a value, prefix, suffix, length, hash, or file path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

UNRECOGNIZED_SETTING = "<unrecognized>"
_SETTING_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ConfigReason(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    NOT_EVALUATED = "not_evaluated"
    MOCK_ADAPTER = "mock_adapter"
    IN_MEMORY_PERSISTENCE = "in_memory_persistence"
    SETTING_INVALID = "setting_invalid"
    SETTING_UNKNOWN = "setting_unknown"
    SETTING_NOT_ALLOWED_IN_SOURCE = "setting_not_allowed_in_source"
    SECRET_IN_SAFE_CONFIGURATION = "secret_in_safe_configuration"  # noqa: S105 - reason code
    ENVIRONMENT_REJECTED = "environment_rejected"
    DUPLICATE_DEFINITION = "duplicate_definition"
    OVERRIDDEN_BY_PROCESS_ENVIRONMENT = "overridden_by_process_environment"
    SECRETS_FILE_PATH_INVALID = "secrets_file_path_invalid"
    SECRETS_FILE_INSIDE_PROTECTED_LOCATION = "secrets_file_inside_protected_location"
    SECRETS_FILE_NOT_FOUND = "secrets_file_not_found"
    SECRETS_FILE_UNREADABLE = "secrets_file_unreadable"
    SECRETS_FILE_TOO_LARGE = "secrets_file_too_large"
    SECRETS_FILE_MALFORMED = "secrets_file_malformed"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_BLANK = "credential_blank"
    CREDENTIAL_PLACEHOLDER = "credential_placeholder"
    CREDENTIAL_MALFORMED = "credential_malformed"
    CREDENTIAL_REF_NOT_ALLOWED = "credential_ref_not_allowed"
    CONNECTION_URL_MISSING = "connection_url_missing"
    PERSISTENCE_MODE_NOT_ALLOWED = "persistence_mode_not_allowed"
    # A configured dependency has no usable implementation or is unreachable.
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    # The database is reachable but its validators/indexes differ from the
    # approved design, or the default configuration version is not stored.
    PERSISTENCE_SCHEMA_MISMATCH = "persistence_schema_mismatch"
    AGENT_CONFIG_NOT_PERSISTED = "agent_config_not_persisted"
    AGENT_CONFIG_NOT_CONFIGURED = "agent_config_not_configured"
    AGENT_CONFIG_NOT_FOUND = "agent_config_not_found"
    AGENT_CONFIG_INVALID = "agent_config_invalid"
    AGENT_CONFIG_CHECKSUM_MISMATCH = "agent_config_checksum_mismatch"
    AGENT_CONFIG_NOT_ACTIVE = "agent_config_not_active"
    AGENT_CONFIG_ENVIRONMENT_MISMATCH = "agent_config_environment_mismatch"
    PROVIDER_NOT_APPROVED = "provider_not_approved"
    PROVIDER_OPTION_NOT_ALLOWED = "provider_option_not_allowed"
    MOCK_ADAPTER_NOT_ALLOWED = "mock_adapter_not_allowed"
    BROWSER_OVERRIDE_REJECTED = "browser_override_rejected"


class ConfigSource(StrEnum):
    PROCESS_ENVIRONMENT = "process_environment"
    EXTERNAL_SECRET_FILE = "external_secret_file"  # noqa: S105 - source label
    SAFE_CONFIGURATION = "safe_configuration"
    DEFAULT = "default"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


def safe_setting_label(name: str) -> str:
    """Return ``name`` only if it is a plain setting identifier."""
    return name if _SETTING_NAME.match(name) else UNRECOGNIZED_SETTING


@dataclass(frozen=True, slots=True)
class ConfigDiagnostic:
    reason: ConfigReason
    setting: str | None = None
    source: ConfigSource | None = None
    severity: Severity = Severity.ERROR
    line: int | None = None

    def to_safe_dict(self) -> dict[str, str | int]:
        data: dict[str, str | int] = {"reason": self.reason.value, "severity": self.severity.value}
        if self.setting is not None:
            data["setting"] = self.setting
        if self.source is not None:
            data["source"] = self.source.value
        if self.line is not None:
            data["line"] = self.line
        return data


class ConfigurationError(Exception):
    """Fail-safe startup error whose text contains only normalized codes."""

    def __init__(self, diagnostics: tuple[ConfigDiagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("a configuration error needs at least one diagnostic")
        self.diagnostics = diagnostics
        super().__init__(self._summary())

    def _summary(self) -> str:
        parts = [
            f"{item.reason.value}({item.setting})" if item.setting else item.reason.value
            for item in self.diagnostics
        ]
        return "configuration invalid: " + ", ".join(parts)

    @property
    def reasons(self) -> tuple[ConfigReason, ...]:
        return tuple(item.reason for item in self.diagnostics)


def diagnostics_from_validation_error(
    error: ValidationError,
    sources: Mapping[str, ConfigSource],
) -> tuple[ConfigDiagnostic, ...]:
    """Map a Pydantic error to diagnostics using only field locations, never input."""
    diagnostics: list[ConfigDiagnostic] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = item["loc"]
        # A provided value is located by its alias; a failing *default* (for
        # example a setting required only in remote mode) by its field name,
        # which is always the lowercase alias.
        name = str(location[0]).upper() if location else ""
        label = safe_setting_label(name) if name else None
        diagnostics.append(
            ConfigDiagnostic(
                reason=ConfigReason.SETTING_INVALID,
                setting=label,
                source=sources.get(name),
            )
        )
    return tuple(diagnostics)
