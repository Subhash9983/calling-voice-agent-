"""Bootstrap configuration loading with the approved precedence (docs/12 §8).

Highest priority first:

1. explicit process environment variable;
2. external R&D secret file named by ``VOICE_AGENT_SECRETS_FILE``;
3. safe environment-specific configuration (caller-supplied mapping);
4. safe application default (``BootstrapSettings`` field defaults).

Loading either returns immutable settings plus value-free provenance and
warnings, or raises ``ConfigurationError`` carrying only normalized codes.
The error is raised outside any ``except`` block so no exception context
holding raw input can be chained onto it.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from voice_agent.security.config_errors import (
    ConfigDiagnostic,
    ConfigReason,
    ConfigSource,
    ConfigurationError,
    Severity,
    diagnostics_from_validation_error,
    safe_setting_label,
)
from voice_agent.security.secret_file import (
    SecretFileError,
    default_protected_roots,
    read_secrets_file,
)
from voice_agent.security.settings import (
    SECRET_ALIASES,
    SECRETS_FILE_VARIABLE,
    SETTING_ALIASES,
    BootstrapSettings,
)

# Environment prefixes owned by this application: an unknown name under one of
# them is a misspelling or an unapproved behaviour override and is rejected.
OWNED_ENVIRONMENT_PREFIXES: tuple[str, ...] = ("APP_", "VOICE_AGENT_")
REJECTED_ENVIRONMENT = "production"


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    settings: BootstrapSettings
    provenance: Mapping[str, ConfigSource]
    diagnostics: tuple[ConfigDiagnostic, ...]


def _environment_values(
    environ: Mapping[str, str], problems: list[ConfigDiagnostic]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_key, value in environ.items():
        key = raw_key.upper()
        if key in SETTING_ALIASES:
            if key in values:
                problems.append(_error(ConfigReason.DUPLICATE_DEFINITION, key, _ENV))
            values[key] = value
        elif key.startswith(OWNED_ENVIRONMENT_PREFIXES):
            problems.append(_error(ConfigReason.SETTING_UNKNOWN, key, _ENV))
    return values


def _safe_values(safe: Mapping[str, str], problems: list[ConfigDiagnostic]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in safe.items():
        if key in SECRET_ALIASES:
            problems.append(_error(ConfigReason.SECRET_IN_SAFE_CONFIGURATION, key, _SAFE))
        elif key == SECRETS_FILE_VARIABLE:
            problems.append(_error(ConfigReason.SETTING_NOT_ALLOWED_IN_SOURCE, key, _SAFE))
        elif key not in SETTING_ALIASES:
            problems.append(_error(ConfigReason.SETTING_UNKNOWN, key, _SAFE))
        else:
            values[key] = value
    return values


def _file_values(
    raw_path: str | None, roots: Sequence[Path], problems: list[ConfigDiagnostic]
) -> dict[str, str]:
    if raw_path is None:
        return {}
    try:
        contents = read_secrets_file(raw_path, roots)
    except SecretFileError as exc:
        problems.extend(exc.diagnostics)
        return {}
    values: dict[str, str] = {}
    for key, value in contents.values.items():
        if key == SECRETS_FILE_VARIABLE:
            problems.append(_error(ConfigReason.SETTING_NOT_ALLOWED_IN_SOURCE, key, _FILE))
        elif key not in SETTING_ALIASES:
            problems.append(_error(ConfigReason.SETTING_UNKNOWN, key, _FILE))
        else:
            values[key] = value
    return values


def _override_warnings(env: Mapping[str, str], file: Mapping[str, str]) -> list[ConfigDiagnostic]:
    """Evidence for secrets defined differently in env and file; values never compared out."""
    return [
        ConfigDiagnostic(
            ConfigReason.OVERRIDDEN_BY_PROCESS_ENVIRONMENT,
            setting=key,
            source=_ENV,
            severity=Severity.WARNING,
        )
        for key in sorted(env.keys() & file.keys())
        if key in SECRET_ALIASES
        and not hmac.compare_digest(env[key].encode("utf-8"), file[key].encode("utf-8"))
    ]


def load_bootstrap_configuration(
    environ: Mapping[str, str] | None = None,
    *,
    safe_configuration: Mapping[str, str] | None = None,
    protected_roots: Sequence[Path] | None = None,
) -> LoadedConfiguration:
    """Load settings; raise ``ConfigurationError`` (codes only) when invalid."""
    source_env = os.environ if environ is None else environ
    roots = default_protected_roots() if protected_roots is None else tuple(protected_roots)
    problems: list[ConfigDiagnostic] = []
    env = _environment_values(source_env, problems)
    safe = _safe_values(safe_configuration or {}, problems)
    file = _file_values(env.get(SECRETS_FILE_VARIABLE), roots, problems)
    if env.get("APP_ENV", file.get("APP_ENV", safe.get("APP_ENV"))) == REJECTED_ENVIRONMENT:
        problems.append(ConfigDiagnostic(ConfigReason.ENVIRONMENT_REJECTED, setting="APP_ENV"))
    if problems:
        raise ConfigurationError(tuple(problems))

    merged = {**safe, **file, **env}
    provenance = _provenance(env, file, safe)
    settings, errors = _construct(merged, provenance)
    if settings is None:
        raise ConfigurationError(errors)
    return LoadedConfiguration(
        settings=settings,
        provenance=MappingProxyType(provenance),
        diagnostics=tuple(_override_warnings(env, file)),
    )


def _construct(
    merged: Mapping[str, str], provenance: Mapping[str, ConfigSource]
) -> tuple[BootstrapSettings | None, tuple[ConfigDiagnostic, ...]]:
    try:
        values: dict[str, Any] = dict(merged)
        return BootstrapSettings(**values), ()
    except ValidationError as exc:
        return None, diagnostics_from_validation_error(exc, provenance)


def _provenance(
    env: Mapping[str, str], file: Mapping[str, str], safe: Mapping[str, str]
) -> dict[str, ConfigSource]:
    def source_of(key: str) -> ConfigSource:
        if key in env:
            return _ENV
        if key in file:
            return _FILE
        if key in safe:
            return _SAFE
        return ConfigSource.DEFAULT

    return {key: source_of(key) for key in sorted(SETTING_ALIASES)}


_ENV = ConfigSource.PROCESS_ENVIRONMENT
_FILE = ConfigSource.EXTERNAL_SECRET_FILE
_SAFE = ConfigSource.SAFE_CONFIGURATION


def _error(reason: ConfigReason, key: str, source: ConfigSource) -> ConfigDiagnostic:
    return ConfigDiagnostic(reason, setting=safe_setting_label(key), source=source)
