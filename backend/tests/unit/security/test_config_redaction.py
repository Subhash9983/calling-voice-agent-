"""Redaction snapshots: synthetic secrets never reach logs, errors, reprs, or serialization.

docs/12 §13, §18. Every canary is synthetic; each test asserts that no
canary (or a prefix/suffix fragment of it) survives in any produced text.
"""

from __future__ import annotations

import json
import logging
import pickle
import traceback
from pathlib import Path
from typing import Any

import pytest

from voice_agent.provider_registry.startup_check import run_startup_check
from voice_agent.security.config_errors import ConfigurationError
from voice_agent.security.config_loader import LoadedConfiguration, load_bootstrap_configuration
from voice_agent.security.diagnostics import redacted_configuration_diagnostics
from voice_agent.security.readiness import PersistenceMode, ProcessRole

MONGO_PASSWORD = "Canary" + "Mongo7Pass"
CANARIES: dict[str, str] = {
    "MONGODB_URI": f"mongodb+srv://canary_user:{MONGO_PASSWORD}@cluster0.canary1.mongodb.net/?authSource=admin",
    "LIVEKIT_API_KEY": "APIcanary" + "Lk0001",
    "LIVEKIT_API_SECRET": "lk-secret-canary-" + "Mm1Nn2Oo3Pp4",
    "DEEPGRAM_API_KEY": "dg-canary-" + "Dd1Ee2Ff3Gg4Hh5",
    "OPENAI_API_KEY": "sk-canary" + "Oo1Pp2Qq3Rr4Ss5Tt6",
    "SARVAM_API_KEY": "sv-canary-" + "Ss1Vv2Ww3Xx4",
    "XAI_API_KEY": "xai-canary-" + "Xx1Aa2Ii3",
    "ELEVENLABS_API_KEY": "el-canary-" + "Ee1Ll2Vv3",
}
LIVEKIT_URL = "wss://canary-topology.livekit.cloud"


def _fragments() -> list[str]:
    values = [*CANARIES.values(), MONGO_PASSWORD, "canary_user", "canary-topology"]
    return values + [v[:8] for v in values] + [v[-8:] for v in values]


def assert_no_secret(text: str) -> None:
    for fragment in _fragments():
        assert fragment not in text, f"secret fragment leaked: {fragment[:3]}***"


@pytest.fixture
def secret_path(tmp_path: Path) -> Path:
    folder = tmp_path / "LocalAppData" / "VoiceAgentRND"
    folder.mkdir(parents=True)
    path = folder / "secrets.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in CANARIES.items()), encoding="utf-8")
    return path


@pytest.fixture
def loaded(secret_path: Path, tmp_path: Path) -> LoadedConfiguration:
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_path), "LIVEKIT_URL": LIVEKIT_URL}
    return load_bootstrap_configuration(environ, protected_roots=[tmp_path / "workspace"])


def test_settings_repr_str_and_serialization_mask_every_secret(
    loaded: LoadedConfiguration,
) -> None:
    settings = loaded.settings

    for text in (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        repr(settings.model_dump()),
        json.dumps(settings.model_dump(mode="json")),
        repr(loaded),
    ):
        assert_no_secret(text)
        assert "secrets.env" not in text


def test_redacted_diagnostics_snapshot(loaded: LoadedConfiguration) -> None:
    diagnostics = redacted_configuration_diagnostics(loaded)

    assert_no_secret(json.dumps(diagnostics))
    assert diagnostics["livekit_url_configured"] is True
    assert diagnostics["secrets_file_configured"] is True
    assert diagnostics["credentials"]["OPENAI_API_KEY"] == {  # type: ignore[index]
        "credential_available": True,
        "credential_status": "available",
        "credential_source": "external_secret_file",
    }
    assert diagnostics["sources"]["LIVEKIT_URL"] == "process_environment"  # type: ignore[index]
    assert diagnostics["warnings"] == []


def test_diagnostics_for_unconfigured_credentials(tmp_path: Path) -> None:
    loaded = load_bootstrap_configuration({}, protected_roots=[tmp_path])

    diagnostics = redacted_configuration_diagnostics(loaded)

    assert diagnostics["credentials"]["SARVAM_API_KEY"] == {  # type: ignore[index]
        "credential_available": False,
        "credential_status": "missing",
        "credential_source": "not_configured",
    }
    assert diagnostics["secrets_file_configured"] is False


def _failing_error(secret_path: Path, tmp_path: Path) -> ConfigurationError:
    text = secret_path.read_text(encoding="utf-8")
    secret_path.write_text(
        text + "APP_API_PORT=" + CANARIES["OPENAI_API_KEY"] + "\n" + CANARIES["SARVAM_API_KEY"],
        encoding="utf-8",
    )
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_path)}
    with pytest.raises(ConfigurationError) as caught:
        load_bootstrap_configuration(environ, protected_roots=[tmp_path / "workspace"])
    return caught.value


def test_configuration_error_and_traceback_never_contain_secrets(
    secret_path: Path, tmp_path: Path
) -> None:
    error = _failing_error(secret_path, tmp_path)

    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), repr(error.args), repr(error.diagnostics), rendered):
        assert_no_secret(text)
        assert str(secret_path.parent) not in text


def test_invalid_setting_value_is_not_echoed(tmp_path: Path) -> None:
    environ = {
        "APP_API_PORT": CANARIES["OPENAI_API_KEY"],
        "LIVEKIT_URL": "https://" + CANARIES["SARVAM_API_KEY"],
    }

    with pytest.raises(ConfigurationError) as caught:
        load_bootstrap_configuration(environ, protected_roots=[tmp_path])

    rendered = "".join(traceback.format_exception(caught.value))
    assert_no_secret(rendered)
    assert caught.value.__context__ is None
    assert {d.setting for d in caught.value.diagnostics} == {"APP_API_PORT", "LIVEKIT_URL"}


def test_startup_logs_and_report_are_secret_free(
    secret_path: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {"VOICE_AGENT_SECRETS_FILE": str(secret_path), "LIVEKIT_URL": LIVEKIT_URL}

    with caplog.at_level(logging.DEBUG):
        outcome = run_startup_check(
            ProcessRole.AGENT_WORKER,
            persistence=PersistenceMode.MONGODB,
            environ=environ,
            protected_roots=[tmp_path / "workspace"],
        )

    assert caplog.records, "startup must emit safe readiness evidence"
    assert_no_secret(caplog.text)
    assert_no_secret(json.dumps(outcome.to_safe_dict()))
    assert_no_secret(repr(outcome.report))


def test_settings_cannot_be_pickled_into_plain_text_by_accident(
    loaded: LoadedConfiguration,
) -> None:
    # Pickle is not a logging path; this documents that SecretStr keeps the
    # value in memory (it is masked in text, not encrypted).
    restored: Any = pickle.loads(pickle.dumps(loaded.settings))  # noqa: S301 - local round trip

    assert_no_secret(repr(restored))
