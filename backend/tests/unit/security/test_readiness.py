"""Configuration readiness exposes only normalized safe codes (docs/12 §5, §12, §18)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent.provider_registry.approved import check_agent_config
from voice_agent.provider_registry.mock_config import (
    MOCK_AGENT_CONFIG_ID,
    mock_agent_config_document,
)
from voice_agent.provider_registry.startup_check import run_startup_check
from voice_agent.security.config_errors import (
    ConfigDiagnostic,
    ConfigReason,
    ConfigurationError,
)
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.readiness import (
    PersistenceMode,
    ProcessRole,
    ReadinessComponent,
    ReadinessStatus,
    evaluate_readiness,
)
from voice_agent.security.settings import AppEnvironment

BASELINE_ID = "11111111-1111-4111-8111-111111111111"
C = ReadinessComponent
S = ReadinessStatus
WORKER = ProcessRole.AGENT_WORKER
API = ProcessRole.CONTROL_API
MONGO = PersistenceMode.MONGODB
MEMORY = PersistenceMode.IN_MEMORY

SYNTHETIC_SECRETS = {
    "MONGODB_URI": "mongodb+srv://voice_user:Synth3ticPass@cluster0.abcd1.mongodb.net/",
    "LIVEKIT_API_KEY": "APIsynthetic0001",
    "LIVEKIT_API_SECRET": "livekit-secret-" + "Kk1Ll2Mm3Nn4Oo5Pp6",
    "DEEPGRAM_API_KEY": "0a1b2c3d4e5f" * 3 + "0a1b",
    "OPENAI_API_KEY": "sk-canary" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8",
    "SARVAM_API_KEY": "sarvam-canary-" + "Qq1Rr2Ss3Tt4",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _secret_file(tmp_path: Path, values: dict[str, str]) -> str:
    folder = tmp_path / "external"
    folder.mkdir(exist_ok=True)
    path = folder / "secrets.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return str(path)


def _baseline_environ(tmp_path: Path, **secret_changes: str | None) -> dict[str, str]:
    secrets = {**SYNTHETIC_SECRETS, **secret_changes}
    present = {k: v for k, v in secrets.items() if v is not None}
    return {
        "VOICE_AGENT_SECRETS_FILE": _secret_file(tmp_path, present),
        "APP_DEFAULT_AGENT_CONFIG_ID": BASELINE_ID,
        "LIVEKIT_URL": "wss://synthetic-project.livekit.cloud",
    }


def _run(
    environ: dict[str, str],
    workspace: Path,
    *,
    role: ProcessRole = WORKER,
    persistence: PersistenceMode = MONGO,
    document: dict[str, Any] | None = None,
) -> Any:
    def lookup(config_id: str) -> dict[str, Any] | None:
        if config_id == MOCK_AGENT_CONFIG_ID:
            return mock_agent_config_document()
        return document

    return run_startup_check(
        role,
        persistence=persistence,
        environ=environ,
        lookup=lookup,
        protected_roots=[workspace],
    ).report


def _by_component(report: Any) -> dict[ReadinessComponent, tuple[S, ConfigReason]]:
    return {item.component: (item.status, item.reason) for item in report.components}


@pytest.mark.parametrize("role", list(ProcessRole))
def test_process_starts_with_mock_configuration_and_no_secrets(
    workspace: Path, role: ProcessRole
) -> None:
    report = _run(
        {"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID},
        workspace,
        role=role,
        persistence=MEMORY,
    )

    assert report.ready
    states = _by_component(report)
    assert states[C.PERSISTENCE] == (S.READY, ConfigReason.IN_MEMORY_PERSISTENCE)
    assert states[C.TRANSPORT] == (S.READY, ConfigReason.MOCK_ADAPTER)


def test_worker_baseline_with_all_synthetic_secrets_is_ready(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    report = _run(_baseline_environ(tmp_path), workspace, document=baseline_document())

    assert report.ready, report.to_safe_dict()
    assert {status for status, _ in _by_component(report).values()} == {S.READY}


@pytest.mark.parametrize(
    ("missing", "component"),
    [
        ("SARVAM_API_KEY", C.TTS),
        ("OPENAI_API_KEY", C.CONVERSATION_ENGINE),
        ("DEEPGRAM_API_KEY", C.STT),
        ("LIVEKIT_API_SECRET", C.TRANSPORT),
        ("MONGODB_URI", C.PERSISTENCE),
    ],
)
def test_missing_enabled_key_prevents_worker_readiness(
    workspace: Path, tmp_path: Path, baseline_document: Any, missing: str, component: C
) -> None:
    environ = _baseline_environ(tmp_path, **{missing: None})

    report = _run(environ, workspace, document=baseline_document())

    assert not report.ready
    assert _by_component(report)[component] == (S.NOT_READY, ConfigReason.CREDENTIAL_MISSING)
    assert report.reasons == (ConfigReason.CREDENTIAL_MISSING,)


def test_control_api_does_not_require_provider_keys(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    environ = _baseline_environ(
        tmp_path, DEEPGRAM_API_KEY=None, OPENAI_API_KEY=None, SARVAM_API_KEY=None
    )

    report = _run(environ, workspace, role=API, document=baseline_document())

    assert report.ready
    states = _by_component(report)
    assert states[C.STT] == (S.DISABLED, ConfigReason.DISABLED)
    assert states[C.TTS] == (S.DISABLED, ConfigReason.DISABLED)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", ConfigReason.CREDENTIAL_BLANK),
        ("<sarvam-api-key>", ConfigReason.CREDENTIAL_PLACEHOLDER),
        ("bad key value", ConfigReason.CREDENTIAL_MALFORMED),
    ],
)
def test_blank_example_or_malformed_key_prevents_readiness(
    workspace: Path, tmp_path: Path, baseline_document: Any, value: str, reason: ConfigReason
) -> None:
    raw = _baseline_environ(tmp_path)
    secrets = {**SYNTHETIC_SECRETS, "SARVAM_API_KEY": value}
    path = Path(raw["VOICE_AGENT_SECRETS_FILE"])
    quoted = {k: f'"{v}"' for k, v in secrets.items()}
    path.write_text("".join(f"{k}={v}\n" for k, v in quoted.items()), encoding="utf-8")

    report = _run(raw, workspace, document=baseline_document())

    assert _by_component(report)[C.TTS] == (S.NOT_READY, reason)


def test_malformed_mongodb_uri_prevents_readiness(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    environ = _baseline_environ(tmp_path, MONGODB_URI="http://cluster0.abcd1.mongodb.net")

    report = _run(environ, workspace, role=API, document=baseline_document())

    assert _by_component(report)[C.PERSISTENCE] == (S.NOT_READY, ConfigReason.CREDENTIAL_MALFORMED)


def test_missing_livekit_url_prevents_readiness(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    environ = _baseline_environ(tmp_path)
    del environ["LIVEKIT_URL"]

    report = _run(environ, workspace, role=API, document=baseline_document())

    assert _by_component(report)[C.TRANSPORT] == (S.NOT_READY, ConfigReason.CONNECTION_URL_MISSING)


def test_disabled_challenger_does_not_need_or_validate_its_key(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    environ = _baseline_environ(tmp_path, XAI_API_KEY="changeme")

    report = _run(environ, workspace, document=baseline_document())

    assert report.ready


def test_enabled_challenger_fails_safely(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    document = baseline_document()
    tts = {**document["tts"], "provider": "elevenlabs", "model": "eleven_flash_v2_5"}

    report = _run(_baseline_environ(tmp_path), workspace, document=baseline_document(tts=tts))

    assert not report.ready
    assert _by_component(report)[C.TTS] == (S.NOT_READY, ConfigReason.PROVIDER_NOT_APPROVED)


def test_in_memory_persistence_is_rejected_outside_development(workspace: Path) -> None:
    environ = {"APP_ENV": "rd", "APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID}

    report = _run(environ, workspace, persistence=MEMORY)

    states = _by_component(report)
    assert states[C.PERSISTENCE] == (S.NOT_READY, ConfigReason.PERSISTENCE_MODE_NOT_ALLOWED)
    assert states[C.AGENT_CONFIG] == (S.NOT_READY, ConfigReason.AGENT_CONFIG_ENVIRONMENT_MISMATCH)
    assert states[C.STT] == (S.NOT_READY, ConfigReason.NOT_EVALUATED)


def test_missing_default_config_id_prevents_readiness(workspace: Path) -> None:
    report = _run({}, workspace, persistence=MEMORY)

    assert _by_component(report)[C.AGENT_CONFIG] == (
        S.NOT_READY,
        ConfigReason.AGENT_CONFIG_NOT_CONFIGURED,
    )


def test_invalid_settings_mark_everything_not_ready(workspace: Path) -> None:
    report = _run({"APP_ENV": "production"}, workspace, persistence=MEMORY)

    states = _by_component(report)
    assert states[C.SETTINGS] == (S.NOT_READY, ConfigReason.ENVIRONMENT_REJECTED)
    assert all(
        state == (S.NOT_READY, ConfigReason.NOT_EVALUATED)
        for component, state in states.items()
        if component is not C.SETTINGS
    )


def test_missing_secret_file_fails_readiness_with_safe_code(
    workspace: Path, tmp_path: Path
) -> None:
    environ = {"VOICE_AGENT_SECRETS_FILE": str(tmp_path / "absent.env")}

    report = _run(environ, workspace)

    assert _by_component(report)[C.SETTINGS] == (S.NOT_READY, ConfigReason.SECRETS_FILE_NOT_FOUND)


def test_report_without_agent_check_is_not_ready(workspace: Path) -> None:
    loaded = load_bootstrap_configuration({}, protected_roots=[workspace])

    report = evaluate_readiness(loaded, None, role=API, persistence=MEMORY)

    assert _by_component(report)[C.AGENT_CONFIG] == (S.NOT_READY, ConfigReason.NOT_EVALUATED)


def test_safe_report_contains_only_enum_values(
    workspace: Path, tmp_path: Path, baseline_document: Any
) -> None:
    environ = _baseline_environ(tmp_path, SARVAM_API_KEY=None)
    report = _run(environ, workspace, document=baseline_document())

    safe = report.to_safe_dict()

    assert safe == {
        "role": "agent_worker",
        "ready": False,
        "components": [
            {"component": "settings", "status": "ready", "reason": "ok"},
            {"component": "agent_config", "status": "ready", "reason": "ok"},
            {"component": "persistence", "status": "ready", "reason": "ok"},
            {"component": "transport", "status": "ready", "reason": "ok"},
            {"component": "stt", "status": "ready", "reason": "ok"},
            {"component": "conversation_engine", "status": "ready", "reason": "ok"},
            {"component": "tts", "status": "not_ready", "reason": "credential_missing"},
        ],
    }
    allowed = {m.value for m in ConfigReason} | {m.value for m in C} | {m.value for m in S}
    for item in safe["components"]:  # type: ignore[attr-defined]
        assert set(item.values()) <= allowed


def test_evaluate_readiness_accepts_a_configuration_error() -> None:
    error = ConfigurationError(
        (ConfigDiagnostic(ConfigReason.SETTING_INVALID, setting="APP_API_PORT"),)
    )
    check = check_agent_config(None, expected_config_id=None, app_env=AppEnvironment.DEVELOPMENT)

    report = evaluate_readiness(error, check, role=API, persistence=MEMORY)

    assert report.reasons[0] is ConfigReason.SETTING_INVALID
