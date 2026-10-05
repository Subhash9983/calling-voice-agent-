"""LiveKit SDK placement (docs/06 §2; docs/13 §5; docs/03 §4).

- ``livekit.api`` (server control plane) only in the control-plane adapter;
- ``livekit.rtc`` / ``livekit.agents`` (worker realtime) only in the worker
  transport binding and the worker process glue;
- the control API never imports the worker, ``rtc``, or ``agents``;
- no LiveKit SDK in domain/contracts/ports/orchestration (checked elsewhere too).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import voice_agent

PACKAGE_ROOT = Path(next(iter(voice_agent.__path__)))
CONTROL_PLANE_SDK_ALLOWED = frozenset({"transport_adapters/livekit/control.py"})
WORKER_SDK_ALLOWED = frozenset(
    {
        "transport_adapters/livekit/rtc_binding.py",
        "agent_worker/entrypoint.py",
        "agent_worker/server.py",
    }
)
# The Silero VAD adapter (a later WP) may use the approved plugin extra.
PLUGIN_ALLOWED_PREFIXES = ("speech_activity/",)


def _full_names(source: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def _modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def _livekit_names(path: Path) -> set[str]:
    names = set(_full_names(path.read_text(encoding="utf-8")))
    return {name for name in names if name == "livekit" or name.startswith("livekit.")}


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_livekit_sdk_is_confined_to_its_adapters(path: Path) -> None:
    relative = _relative(path)
    names = _livekit_names(path)
    uses_api = any(n.startswith("livekit.api") for n in names)
    uses_worker = any(n.startswith(("livekit.rtc", "livekit.agents")) for n in names)
    uses_plugins = any(n.startswith("livekit.plugins") for n in names)

    assert not uses_api or relative in CONTROL_PLANE_SDK_ALLOWED
    assert not uses_worker or relative in WORKER_SDK_ALLOWED
    assert not uses_plugins or relative.startswith(PLUGIN_ALLOWED_PREFIXES)


@pytest.mark.parametrize(
    "path",
    sorted((PACKAGE_ROOT / "control_api").rglob("*.py")),
    ids=_relative,
)
def test_control_api_never_reaches_the_worker_or_realtime_sdk(path: Path) -> None:
    names = set(_full_names(path.read_text(encoding="utf-8")))

    assert not any(n.startswith("voice_agent.agent_worker") for n in names)
    assert not any(n.startswith(("livekit.rtc", "livekit.agents")) for n in names)
    assert not any(n.startswith("voice_agent.transport_adapters.livekit.session") for n in names)


def test_scanner_resolves_from_imports() -> None:
    names = set(_full_names("from livekit import api, rtc\nimport livekit.agents"))

    assert {"livekit.api", "livekit.rtc", "livekit.agents"} <= names


def test_confinement_scan_is_non_vacuous() -> None:
    users = {_relative(p) for p in _modules() if _livekit_names(p)}

    assert users >= CONTROL_PLANE_SDK_ALLOWED
    assert "transport_adapters/livekit/rtc_binding.py" in users


# WP10 (docs/05 §11, docs/14 §16): the application owns turn-taking. LiveKit's
# voice pipeline (``AgentSession``/``Agent``) and its semantic/audio Turn
# Detector are never used; the worker uses ``livekit.agents`` only for the
# job server and job context.
FORBIDDEN_LIVEKIT_MODULE_PREFIXES = ("livekit.agents.voice", "livekit.plugins.turn_detector")
FORBIDDEN_LIVEKIT_NAMES = frozenset(
    {"AgentSession", "Agent", "AgentTask", "MultilingualModel", "EnglishModel", "TurnDetector"}
)


def _turn_taking_violations(source: str) -> list[str]:
    found = [
        name for name in _full_names(source) if name.startswith(FORBIDDEN_LIVEKIT_MODULE_PREFIXES)
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("livekit"):
            found.extend(a.name for a in node.names if a.name in FORBIDDEN_LIVEKIT_NAMES)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_LIVEKIT_NAMES:
            found.append(node.attr)
    return found


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_no_livekit_agent_session_or_turn_detector(path: Path) -> None:
    assert _turn_taking_violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        "from livekit.agents import AgentSession",
        "from livekit.agents.voice import Agent",
        "from livekit.plugins.turn_detector.multilingual import MultilingualModel",
        "from livekit import agents\nsession = agents.AgentSession()",
        "import livekit.plugins.turn_detector",
    ],
)
def test_turn_taking_scanner_detects_the_livekit_voice_pipeline(source: str) -> None:
    assert _turn_taking_violations(source)


def test_turn_taking_scanner_allows_the_job_server() -> None:
    source = "from livekit import agents\nserver = agents.AgentServer()\nagents.JobContext"

    assert _turn_taking_violations(source) == []
