"""Bootstrap settings sources, precedence, and fail-safe loading (docs/12 §3, §4, §6, §8).

All secret values are synthetic canaries. Secret files are written only to
pytest's temporary directory, never to the repository or the real location.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_agent.security.config_errors import (
    ConfigReason,
    ConfigSource,
    ConfigurationError,
    Severity,
)
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import AppEnvironment, BootstrapSettings

CANARY_OPENAI = "sk-canary" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8"
CANARY_OTHER = "sk-canary" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def external(tmp_path: Path) -> Path:
    folder = tmp_path / "LocalAppData" / "VoiceAgentRND"
    folder.mkdir(parents=True)
    return folder


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _load(
    environ: dict[str, str],
    workspace: Path,
    safe: dict[str, str] | None = None,
) -> tuple[BootstrapSettings, dict[str, ConfigSource], tuple[object, ...]]:
    loaded = load_bootstrap_configuration(
        environ, safe_configuration=safe, protected_roots=[workspace]
    )
    return loaded.settings, dict(loaded.provenance), loaded.diagnostics


def _reasons(error: ConfigurationError) -> set[ConfigReason]:
    return {item.reason for item in error.diagnostics}


def test_defaults_match_docs_12_section_6(workspace: Path) -> None:
    settings, provenance, diagnostics = _load({}, workspace)

    assert settings.app_env is AppEnvironment.DEVELOPMENT
    assert settings.app_log_level == "INFO"
    assert settings.app_api_host == "127.0.0.1"
    assert settings.app_api_port == 8000
    assert settings.app_public_origin == "http://127.0.0.1:5173"
    assert settings.app_agent_name == "phase0-voice-agent"
    assert settings.mongodb_database == "voice_agent_rnd"
    assert settings.app_default_agent_config_id is None
    assert settings.livekit_url is None
    assert settings.openai_api_key is None
    assert set(provenance.values()) == {ConfigSource.DEFAULT}
    assert diagnostics == ()


def test_precedence_env_over_secret_file_over_safe_config_over_default(
    workspace: Path, external: Path
) -> None:
    secret_file = _write(
        external / "secrets.env",
        "APP_LOG_LEVEL=WARNING\nAPP_API_PORT=8100\nOPENAI_API_KEY=" + CANARY_OPENAI + "\n",
    )
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_file), "APP_LOG_LEVEL": "ERROR"}
    safe = {"APP_LOG_LEVEL": "DEBUG", "APP_API_PORT": "8200", "APP_AGENT_NAME": "safe-agent"}

    settings, provenance, _ = _load(environ, workspace, safe)

    assert settings.app_log_level == "ERROR"
    assert provenance["APP_LOG_LEVEL"] is ConfigSource.PROCESS_ENVIRONMENT
    assert settings.app_api_port == 8100
    assert provenance["APP_API_PORT"] is ConfigSource.EXTERNAL_SECRET_FILE
    assert settings.app_agent_name == "safe-agent"
    assert provenance["APP_AGENT_NAME"] is ConfigSource.SAFE_CONFIGURATION
    assert provenance["APP_ENV"] is ConfigSource.DEFAULT
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == CANARY_OPENAI


def test_conflicting_duplicate_secret_creates_safe_warning(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", f"OPENAI_API_KEY={CANARY_OPENAI}\n")
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_file), "OPENAI_API_KEY": CANARY_OTHER}

    settings, _, diagnostics = _load(environ, workspace)

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == CANARY_OTHER
    assert len(diagnostics) == 1
    warning = diagnostics[0]
    assert warning.reason is ConfigReason.OVERRIDDEN_BY_PROCESS_ENVIRONMENT  # type: ignore[attr-defined]
    assert warning.severity is Severity.WARNING  # type: ignore[attr-defined]
    assert CANARY_OPENAI not in repr(diagnostics)
    assert CANARY_OTHER not in repr(diagnostics)


def test_identical_duplicate_secret_is_not_a_conflict(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", f"OPENAI_API_KEY={CANARY_OPENAI}\n")
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_file), "OPENAI_API_KEY": CANARY_OPENAI}

    _, _, diagnostics = _load(environ, workspace)

    assert diagnostics == ()


def test_secret_file_location_only_from_process_environment(workspace: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({}, workspace, safe={"VOICE_AGENT_SECRETS_FILE": "C:/elsewhere/secrets.env"})

    assert _reasons(caught.value) == {ConfigReason.SETTING_NOT_ALLOWED_IN_SOURCE}


def test_secret_file_cannot_redirect_itself(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", "VOICE_AGENT_SECRETS_FILE=C:/other.env\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SETTING_NOT_ALLOWED_IN_SOURCE}


def test_secret_in_safe_configuration_is_rejected_without_value(workspace: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({}, workspace, safe={"OPENAI_API_KEY": CANARY_OPENAI})

    assert _reasons(caught.value) == {ConfigReason.SECRET_IN_SAFE_CONFIGURATION}
    assert CANARY_OPENAI not in str(caught.value)
    assert CANARY_OPENAI not in repr(caught.value)


def test_missing_secret_file_fails_safely(workspace: Path, external: Path) -> None:
    missing = external / "absent.env"

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(missing)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_NOT_FOUND}
    assert str(external) not in str(caught.value)


@pytest.mark.parametrize("raw", ["relative/secrets.env", "", "   "])
def test_secret_file_path_must_be_absolute(workspace: Path, raw: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": raw}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_PATH_INVALID}


def test_secret_file_inside_workspace_is_rejected(workspace: Path) -> None:
    inside = _write(workspace / "secrets.env", f"OPENAI_API_KEY={CANARY_OPENAI}\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(inside)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_INSIDE_PROTECTED_LOCATION}


def test_secret_file_under_onedrive_is_rejected(workspace: Path, tmp_path: Path) -> None:
    onedrive = tmp_path / "OneDrive - Example" / "keys"
    onedrive.mkdir(parents=True)
    inside = _write(onedrive / "secrets.env", f"OPENAI_API_KEY={CANARY_OPENAI}\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(inside)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_INSIDE_PROTECTED_LOCATION}


def test_secret_file_must_be_a_regular_file(workspace: Path, external: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(external)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_NOT_FOUND}


def test_malformed_secret_file_reports_line_number_only(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", f"# comment\n\n{CANARY_OPENAI}\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.reason is ConfigReason.SECRETS_FILE_MALFORMED
    assert diagnostic.line == 3
    assert CANARY_OPENAI not in str(caught.value)


def test_duplicate_key_inside_secret_file_is_rejected(workspace: Path, external: Path) -> None:
    secret_file = _write(
        external / "secrets.env",
        f"OPENAI_API_KEY={CANARY_OPENAI}\nOPENAI_API_KEY={CANARY_OTHER}\n",
    )

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.DUPLICATE_DEFINITION}
    assert CANARY_OPENAI not in repr(caught.value)
    assert CANARY_OTHER not in repr(caught.value)


def test_unknown_key_in_secret_file_is_rejected(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", f"OPENAI_API_KY={CANARY_OPENAI}\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.reason is ConfigReason.SETTING_UNKNOWN
    assert diagnostic.setting == "OPENAI_API_KY"


def test_secret_file_values_are_literal_without_interpolation(
    workspace: Path, external: Path
) -> None:
    secret_file = _write(
        external / "secrets.env",
        "OPENAI_API_KEY=\"${HOME}-literal-Aa1Bb2Cc3\"\nAPP_AGENT_NAME='quoted-agent'\n",
    )

    settings, _, _ = _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "${HOME}-literal-Aa1Bb2Cc3"
    assert settings.app_agent_name == "quoted-agent"


def test_oversized_secret_file_is_rejected(workspace: Path, external: Path) -> None:
    secret_file = _write(external / "secrets.env", "# pad\n" * 20_000)

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_TOO_LARGE}


def test_non_utf8_secret_file_is_malformed(workspace: Path, external: Path) -> None:
    secret_file = external / "secrets.env"
    secret_file.write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")

    with pytest.raises(ConfigurationError) as caught:
        _load({"VOICE_AGENT_SECRETS_FILE": str(secret_file)}, workspace)

    assert _reasons(caught.value) == {ConfigReason.SECRETS_FILE_MALFORMED}


@pytest.mark.parametrize("name", ["APP_STT_MODEL", "APP_TTS_VOICE", "VOICE_AGENT_PROMPT"])
def test_behaviour_override_environment_variables_are_rejected(workspace: Path, name: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({name: "gpt-override"}, workspace)

    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.reason is ConfigReason.SETTING_UNKNOWN
    assert diagnostic.setting == name


def test_unrelated_process_environment_is_ignored(workspace: Path) -> None:
    settings, _, diagnostics = _load({"PATH": "C:/Windows", "HOME": "C:/Users/x"}, workspace)

    assert settings.app_env is AppEnvironment.DEVELOPMENT
    assert diagnostics == ()


def test_unknown_safe_configuration_key_is_rejected(workspace: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({}, workspace, safe={"APP_DEBUG_DUMP": "true"})

    assert _reasons(caught.value) == {ConfigReason.SETTING_UNKNOWN}


def test_environment_keys_are_matched_case_insensitively(workspace: Path) -> None:
    settings, provenance, _ = _load({"app_log_level": "DEBUG"}, workspace)

    assert settings.app_log_level == "DEBUG"
    assert provenance["APP_LOG_LEVEL"] is ConfigSource.PROCESS_ENVIRONMENT


def test_production_environment_is_rejected_at_phase0_startup(workspace: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({"APP_ENV": "production"}, workspace)

    assert _reasons(caught.value) == {ConfigReason.ENVIRONMENT_REJECTED}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_ENV", "staging"),
        ("APP_LOG_LEVEL", "TRACE"),
        ("APP_API_HOST", "0.0.0.0"),  # noqa: S104 - asserting rejection
        ("APP_API_HOST", "localhost"),
        ("APP_API_PORT", "0"),
        ("APP_API_PORT", "70000"),
        ("APP_API_PORT", "80.5"),
        ("APP_API_PORT", "+8000"),
        ("APP_PUBLIC_ORIGIN", "*"),
        ("APP_PUBLIC_ORIGIN", "http://localhost:5173"),
        ("APP_PUBLIC_ORIGIN", "http://127.0.0.1:5173/"),
        ("APP_AGENT_NAME", "Bad Name!"),
        ("APP_DEFAULT_AGENT_CONFIG_ID", "not-a-uuid"),
        ("MONGODB_DATABASE", "other_db"),
        ("LIVEKIT_URL", "http://project.livekit.cloud"),
        ("LIVEKIT_URL", "wss://"),
        ("LIVEKIT_URL", "wss://user:pw@project.livekit.cloud"),
        ("LIVEKIT_URL", "wss://project.livekit.cloud/?token=abc"),
    ],
)
def test_invalid_bootstrap_values_fail_with_safe_codes(
    workspace: Path, name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load({name: value}, workspace)

    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.reason is ConfigReason.SETTING_INVALID
    assert diagnostic.setting == name
    assert diagnostic.source is ConfigSource.PROCESS_ENVIRONMENT
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_valid_livekit_url_and_config_id_are_accepted(workspace: Path) -> None:
    config_id = "11111111-1111-4111-8111-111111111111"
    environ = {
        "LIVEKIT_URL": "wss://project-abc.livekit.cloud",
        "APP_DEFAULT_AGENT_CONFIG_ID": config_id,
        "APP_ENV": "rd",
    }

    settings, _, _ = _load(environ, workspace)

    assert settings.livekit_url == "wss://project-abc.livekit.cloud"
    assert settings.app_default_agent_config_id == config_id
    assert settings.app_env is AppEnvironment.RD


def test_settings_are_immutable(workspace: Path) -> None:
    settings, _, _ = _load({}, workspace)

    with pytest.raises(ValidationError):
        settings.app_api_port = 9000  # type: ignore[misc]


def test_direct_construction_reads_no_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_API_PORT", "9999")

    assert BootstrapSettings().app_api_port == 8000


def test_default_loader_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    # Never let the developer's real secret-file reference or settings leak in.
    for key in list(os.environ):
        if key.upper().startswith(("APP_", "VOICE_AGENT_", "MONGODB_", "LIVEKIT_")) or (
            key.upper().endswith("_API_KEY")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_API_PORT", "8123")

    loaded = load_bootstrap_configuration(protected_roots=[workspace])

    assert loaded.settings.app_api_port == 8123
