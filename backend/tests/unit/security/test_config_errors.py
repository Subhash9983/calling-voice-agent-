"""Value-free diagnostic shapes and small safety helpers (docs/12 §12-§13)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from voice_agent.security.config_errors import (
    ConfigDiagnostic,
    ConfigReason,
    ConfigSource,
    ConfigurationError,
    Severity,
    safe_setting_label,
)
from voice_agent.security.credentials import (
    CredentialResolver,
    CredentialStatus,
    classify_credential,
)
from voice_agent.security.secret_file import (
    SecretFileError,
    default_protected_roots,
    parse_secrets_text,
)
from voice_agent.security.settings import BootstrapSettings


def test_diagnostic_safe_dict_includes_only_present_fields() -> None:
    full = ConfigDiagnostic(
        ConfigReason.SECRETS_FILE_MALFORMED,
        setting="OPENAI_API_KEY",
        source=ConfigSource.EXTERNAL_SECRET_FILE,
        severity=Severity.WARNING,
        line=4,
    )

    assert full.to_safe_dict() == {
        "reason": "secrets_file_malformed",
        "severity": "warning",
        "setting": "OPENAI_API_KEY",
        "source": "external_secret_file",
        "line": 4,
    }
    assert ConfigDiagnostic(ConfigReason.OK).to_safe_dict() == {"reason": "ok", "severity": "error"}


def test_configuration_error_summary_and_reasons() -> None:
    error = ConfigurationError(
        (
            ConfigDiagnostic(ConfigReason.SETTING_INVALID, setting="APP_API_PORT"),
            ConfigDiagnostic(ConfigReason.SECRETS_FILE_NOT_FOUND),
        )
    )

    assert str(error) == (
        "configuration invalid: setting_invalid(APP_API_PORT), secrets_file_not_found"
    )
    assert error.reasons == (ConfigReason.SETTING_INVALID, ConfigReason.SECRETS_FILE_NOT_FOUND)


def test_configuration_error_requires_a_diagnostic() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ConfigurationError(())


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("sk-abc", "<unrecognized>"),
        ("A" * 65, "<unrecognized>"),
    ],
)
def test_safe_setting_label(name: str, label: str) -> None:
    assert safe_setting_label(name) == label


def test_quoted_value_containing_its_quote_is_malformed() -> None:
    with pytest.raises(SecretFileError) as caught:
        parse_secrets_text('OPENAI_API_KEY="a"b"\n')

    assert caught.value.diagnostics[0].line == 1


def test_blank_value_is_kept_for_readiness_classification() -> None:
    assert parse_secrets_text("SARVAM_API_KEY=\n").values == {"SARVAM_API_KEY": ""}


def test_default_protected_roots_include_workspace_and_onedrive(tmp_path: Path) -> None:
    roots = default_protected_roots({"OneDrive": str(tmp_path / "OneDrive")})

    assert tmp_path / "OneDrive" in roots
    assert any((root / ".git").exists() for root in roots)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("mongodb://u:p1234567@[::1]:27017/", CredentialStatus.AVAILABLE),
        ("mongodb://u:p1234567@[::1]/", CredentialStatus.AVAILABLE),
        ("mongodb://u:p1234567@[::1]x/", CredentialStatus.MALFORMED),
        ("mongodb://u:p1234567@h1:27017,h2:27018/", CredentialStatus.AVAILABLE),
        ("mongodb://u:p1234567@h1:port/", CredentialStatus.MALFORMED),
    ],
)
def test_mongodb_host_forms(uri: str, expected: CredentialStatus) -> None:
    assert classify_credential("MONGODB_URI", SecretStr(uri)) is expected


def test_resolver_repr_and_unknown_secret_lookup() -> None:
    settings = BootstrapSettings()

    assert repr(CredentialResolver(settings)) == "CredentialResolver()"
    with pytest.raises(KeyError):
        settings.secret("PATH")
