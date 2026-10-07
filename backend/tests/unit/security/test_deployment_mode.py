"""Decision 070 limited-sharing deployment mode settings (local default unchanged)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from voice_agent.security.config_errors import ConfigReason, ConfigSource, ConfigurationError
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import (
    DEFAULT_DAILY_SPEND_CAP_INR,
    BootstrapSettings,
    DeploymentMode,
)

ANY_IPV4 = "0.0.0.0"  # noqa: S104 - the binding under test
ORIGIN = "https://voice-agent-web.onrender.com"
API_HOST = "voice-agent-api.onrender.com"
REMOTE = {
    "APP_DEPLOYMENT_MODE": "remote_limited_sharing",
    "APP_API_HOST": ANY_IPV4,
    "APP_PUBLIC_ORIGIN": ORIGIN,
    "APP_API_PUBLIC_HOST": API_HOST,
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _load(environ: dict[str, str], workspace: Path) -> BootstrapSettings:
    return load_bootstrap_configuration(environ, protected_roots=[workspace]).settings


def _invalid_setting(
    environ: dict[str, str],
    workspace: Path,
    source: ConfigSource = ConfigSource.PROCESS_ENVIRONMENT,
) -> str | None:
    with pytest.raises(ConfigurationError) as caught:
        _load(environ, workspace)
    (diagnostic,) = caught.value.diagnostics
    assert diagnostic.reason is ConfigReason.SETTING_INVALID
    assert diagnostic.source is source
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    return diagnostic.setting


# ------------------------------------------------------------ local mode --


def test_local_mode_is_the_default_and_unchanged(workspace: Path) -> None:
    settings = _load({}, workspace)

    assert settings.app_deployment_mode is DeploymentMode.LOCAL
    assert settings.app_api_host == "127.0.0.1"
    assert settings.app_public_origin == "http://127.0.0.1:5173"
    assert settings.app_api_public_host is None
    assert settings.app_daily_spend_cap_inr == DEFAULT_DAILY_SPEND_CAP_INR == Decimal("200.00")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_API_HOST", ANY_IPV4),
        ("APP_API_HOST", "localhost"),
        ("APP_API_HOST", "::"),
        ("APP_PUBLIC_ORIGIN", ORIGIN),
        ("APP_PUBLIC_ORIGIN", "*"),
        ("APP_PUBLIC_ORIGIN", "http://localhost:5173"),
        ("APP_API_PUBLIC_HOST", API_HOST),
        ("APP_DEPLOYMENT_MODE", "public"),
        ("APP_DEPLOYMENT_MODE", "REMOTE_LIMITED_SHARING"),
    ],
)
def test_local_mode_rejects_every_remote_value(workspace: Path, name: str, value: str) -> None:
    assert _invalid_setting({name: value}, workspace) == name


def test_explicit_local_mode_matches_the_default(workspace: Path) -> None:
    assert _load({"APP_DEPLOYMENT_MODE": "local"}, workspace) == _load({}, workspace)


# ----------------------------------------------------------- remote mode --


def test_remote_mode_accepts_any_ipv4_bind_and_the_https_origin(workspace: Path) -> None:
    settings = _load(REMOTE, workspace)

    assert settings.app_deployment_mode is DeploymentMode.REMOTE_LIMITED_SHARING
    assert settings.app_api_host == ANY_IPV4
    assert settings.app_public_origin == ORIGIN
    assert settings.app_api_public_host == API_HOST


def test_remote_mode_may_keep_the_loopback_bind(workspace: Path) -> None:
    settings = _load({**REMOTE, "APP_API_HOST": "127.0.0.1"}, workspace)

    assert settings.app_api_host == "127.0.0.1"


@pytest.mark.parametrize(
    "origin",
    [
        "http://voice-agent-web.onrender.com",
        "http://127.0.0.1:5173",
        "*",
        "https://*.onrender.com",
        "https://*",
        "https://",
        "https://voice-agent-web.onrender.com/",
        "https://voice-agent-web.onrender.com/app",
        "https://voice-agent-web.onrender.com?x=1",
        "https://voice-agent-web.onrender.com#frag",
        "https://user:pw@voice-agent-web.onrender.com",
        "https://Voice-Agent-Web.onrender.com",
        "https://voice agent.onrender.com",
        "wss://voice-agent-web.onrender.com",
    ],
)
def test_remote_mode_rejects_non_https_wildcard_and_malformed_origins(
    workspace: Path, origin: str
) -> None:
    environ = {**REMOTE, "APP_PUBLIC_ORIGIN": origin}

    assert _invalid_setting(environ, workspace) == "APP_PUBLIC_ORIGIN"


def test_remote_mode_accepts_an_https_origin_with_an_explicit_port(workspace: Path) -> None:
    settings = _load({**REMOTE, "APP_PUBLIC_ORIGIN": "https://tester.example.org:8443"}, workspace)

    assert settings.app_public_origin == "https://tester.example.org:8443"


def test_remote_mode_requires_an_explicit_public_origin(workspace: Path) -> None:
    environ = {k: v for k, v in REMOTE.items() if k != "APP_PUBLIC_ORIGIN"}

    assert _invalid_setting(environ, workspace, ConfigSource.DEFAULT) == "APP_PUBLIC_ORIGIN"


def test_remote_mode_requires_the_public_api_host(workspace: Path) -> None:
    environ = {k: v for k, v in REMOTE.items() if k != "APP_API_PUBLIC_HOST"}

    assert _invalid_setting(environ, workspace, ConfigSource.DEFAULT) == "APP_API_PUBLIC_HOST"


@pytest.mark.parametrize(
    "host",
    [
        "*",
        "*.onrender.com",
        "https://voice-agent-api.onrender.com",
        "voice-agent-api.onrender.com:443",
        "voice-agent-api.onrender.com/",
        "Voice-Agent-Api.onrender.com",
        "-bad.onrender.com",
        "localhost",
        "127.0.0.1",
        ANY_IPV4,
    ],
)
def test_remote_mode_rejects_unsafe_public_api_hosts(workspace: Path, host: str) -> None:
    environ = {**REMOTE, "APP_API_PUBLIC_HOST": host}

    assert _invalid_setting(environ, workspace) == "APP_API_PUBLIC_HOST"


@pytest.mark.parametrize("host", ["::", "localhost", "192.168.1.10"])
def test_remote_mode_still_rejects_other_bind_hosts(workspace: Path, host: str) -> None:
    environ = {**REMOTE, "APP_API_HOST": host}

    assert _invalid_setting(environ, workspace) == "APP_API_HOST"


def test_remote_mode_loads_from_process_environment_only(workspace: Path) -> None:
    """Render offers environment variables only: no secrets file is needed."""
    environ = {**REMOTE, "OPENAI_API_KEY": "sk-test" + "Aa1Bb2Cc3Dd4Ee5Ff6"}

    loaded = load_bootstrap_configuration(environ, protected_roots=[workspace])

    assert loaded.settings.voice_agent_secrets_file is None
    assert loaded.provenance["OPENAI_API_KEY"] is ConfigSource.PROCESS_ENVIRONMENT
    assert loaded.diagnostics == ()


# ------------------------------------------------------------ spend cap --


@pytest.mark.parametrize("value", ["150", "200.00", "0.01"])
def test_spend_cap_accepts_values_up_to_the_decision_070_cap(workspace: Path, value: str) -> None:
    settings = _load({**REMOTE, "APP_DAILY_SPEND_CAP_INR": value}, workspace)

    assert settings.app_daily_spend_cap_inr == Decimal(value)


@pytest.mark.parametrize("value", ["200.01", "1000", "0", "-1", "NaN", "inf", "abc", "1.001"])
def test_spend_cap_rejects_values_outside_the_approved_range(workspace: Path, value: str) -> None:
    environ = {**REMOTE, "APP_DAILY_SPEND_CAP_INR": value}

    assert _invalid_setting(environ, workspace) == "APP_DAILY_SPEND_CAP_INR"
