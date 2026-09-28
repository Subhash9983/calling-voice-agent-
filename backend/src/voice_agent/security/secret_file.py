"""External R&D secret-file validation and strict parsing (docs/12 §4, §8).

The file is only ever read: never created, written, copied, or watched. The
format is a deliberately small ``KEY=VALUE`` subset with no interpolation,
templating, command execution, ``export`` prefix, or multi-line values.
Errors identify a line number or setting name, never a value or the path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from voice_agent.security.config_errors import (
    ConfigDiagnostic,
    ConfigReason,
    ConfigSource,
    safe_setting_label,
)

MAX_SECRETS_FILE_BYTES = 64 * 1024
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ONEDRIVE_ENVIRONMENT_KEYS: tuple[str, ...] = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_SOURCE = ConfigSource.EXTERNAL_SECRET_FILE


class SecretFileError(Exception):
    """A secret-file problem; the message is only the normalized reason code."""

    def __init__(self, diagnostics: tuple[ConfigDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(", ".join(item.reason.value for item in diagnostics))


def _fail(reason: ConfigReason, *, line: int | None = None, setting: str | None = None) -> NoReturn:
    raise SecretFileError((ConfigDiagnostic(reason, setting=setting, source=_SOURCE, line=line),))


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path))


def _repository_root() -> Path | None:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    return None


def default_protected_roots(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """The Git workspace plus every configured OneDrive folder."""
    env = os.environ if environ is None else environ
    roots = [Path(env[key]) for key in _ONEDRIVE_ENVIRONMENT_KEYS if env.get(key)]
    repository = _repository_root()
    if repository is not None:
        roots.append(repository)
    return tuple(roots)


def _is_within(path: Path, root: Path) -> bool:
    child, parent = _normalized(path), _normalized(root.resolve()).rstrip("\\/")
    return child == parent or child.startswith(parent + os.sep)


def _in_onedrive_folder(path: Path) -> bool:
    return any(part.lower().startswith("onedrive") for part in path.parts)


def validate_secrets_file_path(raw: str, protected_roots: Iterable[Path]) -> Path:
    """Return the resolved path of a regular external file, or raise ``SecretFileError``."""
    candidate = Path(raw.strip()) if raw.strip() else None
    if candidate is None or not candidate.is_absolute() or raw.startswith(("\\\\", "//")):
        _fail(ConfigReason.SECRETS_FILE_PATH_INVALID)
    resolved = candidate.resolve()
    if _in_onedrive_folder(resolved) or any(_is_within(resolved, r) for r in protected_roots):
        _fail(ConfigReason.SECRETS_FILE_INSIDE_PROTECTED_LOCATION)
    if candidate.is_symlink() or not resolved.is_file():
        _fail(ConfigReason.SECRETS_FILE_NOT_FOUND)
    return resolved


def _read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_SECRETS_FILE_BYTES:
            _fail(ConfigReason.SECRETS_FILE_TOO_LARGE)
        data = path.read_bytes()
    except OSError:
        _fail(ConfigReason.SECRETS_FILE_UNREADABLE)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(ConfigReason.SECRETS_FILE_MALFORMED)


def _unquote(value: str) -> str | None:
    if not value:
        return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return None if value[0] in inner else inner
    if any(ch.isspace() for ch in value) or value[:1] in "\"'":
        return None
    return value


@dataclass(frozen=True, slots=True)
class SecretFileContents:
    values: Mapping[str, str]


def parse_secrets_text(text: str) -> SecretFileContents:
    """Parse ``KEY=VALUE`` lines; duplicates and malformed lines are rejected."""
    values: dict[str, str] = {}
    problems: list[ConfigDiagnostic] = []
    for number, raw_line in enumerate(text.lstrip("﻿").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        value = _unquote(raw_value.strip())
        if not separator or not _KEY.match(key) or value is None:
            problems.append(
                ConfigDiagnostic(ConfigReason.SECRETS_FILE_MALFORMED, source=_SOURCE, line=number)
            )
            continue
        if key in values:
            problems.append(
                ConfigDiagnostic(
                    ConfigReason.DUPLICATE_DEFINITION,
                    setting=safe_setting_label(key),
                    source=_SOURCE,
                    line=number,
                )
            )
            continue
        values[key] = value
    if problems:
        raise SecretFileError(tuple(problems))
    return SecretFileContents(values=values)


def read_secrets_file(raw_path: str, protected_roots: Sequence[Path]) -> SecretFileContents:
    """Validate the configured location, then read and parse the file once."""
    path = validate_secrets_file_path(raw_path, protected_roots)
    return parse_secrets_text(_read_text(path))
