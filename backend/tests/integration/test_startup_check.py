"""A real child process starts with mock configuration and fails safely otherwise.

The child receives a minimal explicit environment, so the developer's real
``VOICE_AGENT_SECRETS_FILE`` (if any) is never inherited or read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID
from voice_agent.provider_registry.startup_check import main

_PLATFORM_KEYS = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")


def _child_env(**settings: str) -> dict[str, str]:
    base = {key: os.environ[key] for key in _PLATFORM_KEYS if key in os.environ}
    return {**base, **settings}


def _run(role: str, **settings: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "voice_agent.provider_registry.startup_check",
        "--role",
        role,
        "--persistence",
        "in_memory",
    ]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        env=_child_env(**settings),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("role", ["control_api", "agent_worker"])
def test_process_starts_ready_with_mock_configuration(role: str) -> None:
    result = _run(role, APP_DEFAULT_AGENT_CONFIG_ID=MOCK_AGENT_CONFIG_ID)

    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["role"] == role
    assert report["configuration"]["secrets_file_configured"] is False
    assert result.stderr == ""


def test_process_fails_safely_for_invalid_configuration() -> None:
    result = _run("agent_worker", APP_ENV="production", APP_API_PORT="not-a-port")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["components"][0] == {
        "component": "settings",
        "status": "not_ready",
        "reason": "environment_rejected",
    }
    assert "configuration" not in report
    assert "Traceback" not in result.stderr
    assert "not-a-port" not in result.stdout + result.stderr


def test_main_in_process_returns_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    ready = main(
        ["--role", "control_api", "--persistence", "in_memory"],
        environ={"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID},
    )
    not_ready = main(["--role", "control_api"], environ={})

    assert (ready, not_ready) == (0, 1)
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["ready"] for line in lines] == [True, False]
