"""Dependency-direction guard (docs/03 §4).

``domain``, ``contracts`` and ``ports`` must never import FastAPI/Starlette,
LiveKit, PyMongo/BSON/Motor, provider SDKs, or the local ONNX runtime, and
must not reach outward into adapter or entry-point packages.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import voice_agent

PACKAGE_ROOT = Path(next(iter(voice_agent.__path__)))
CORE_PACKAGES: tuple[str, ...] = ("domain", "contracts", "ports")

FORBIDDEN_EXTERNAL_ROOTS: frozenset[str] = frozenset(
    {
        "fastapi",
        "starlette",
        "uvicorn",
        "livekit",
        "pymongo",
        "bson",
        "gridfs",
        "motor",
        "openai",
        "deepgram",
        "sarvamai",
        "onnxruntime",
    }
)

FORBIDDEN_INTERNAL_PACKAGES: frozenset[str] = frozenset(
    {
        "control_api",
        "agent_worker",
        "orchestration",
        "provider_registry",
        "transport_adapters",
        "stt_adapters",
        "conversation_adapters",
        "tts_adapters",
        "persistence",
    }
)


def _imported_modules(source: str, module_package: str) -> Iterator[str]:
    """Yield absolute dotted names imported by ``source``.

    ``module_package`` is the dotted package containing the module, used to
    resolve relative imports.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield _resolve_from_import(node, module_package)


def _resolve_from_import(node: ast.ImportFrom, module_package: str) -> str:
    if node.level == 0:
        return node.module or ""
    base_parts = module_package.split(".")
    anchor = base_parts[: len(base_parts) - (node.level - 1)]
    return ".".join([*anchor, node.module] if node.module else anchor)


def _violations(source: str, module_package: str) -> list[str]:
    return [name for name in _imported_modules(source, module_package) if _is_forbidden(name)]


def _is_forbidden(name: str) -> bool:
    parts = name.split(".")
    if parts[0] in FORBIDDEN_EXTERNAL_ROOTS:
        return True
    return parts[0] == "voice_agent" and len(parts) > 1 and parts[1] in FORBIDDEN_INTERNAL_PACKAGES


def _core_modules() -> list[Path]:
    return sorted(path for pkg in CORE_PACKAGES for path in (PACKAGE_ROOT / pkg).rglob("*.py"))


def _package_of(path: Path) -> str:
    # Both ``pkg/__init__.py`` and ``pkg/module.py`` resolve relative imports
    # against ``pkg``, i.e. the containing directory.
    return ".".join(path.relative_to(PACKAGE_ROOT.parent).parent.parts)


def test_core_packages_exist() -> None:
    for pkg in CORE_PACKAGES:
        assert (PACKAGE_ROOT / pkg / "__init__.py").is_file()


@pytest.mark.parametrize(
    "path", _core_modules(), ids=lambda p: p.relative_to(PACKAGE_ROOT).as_posix()
)
def test_core_module_has_no_forbidden_imports(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    assert _violations(source, _package_of(path)) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import fastapi", ["fastapi"]),
        ("from livekit import rtc", ["livekit"]),
        ("from livekit.plugins import silero", ["livekit.plugins"]),
        ("import pymongo.asynchronous", ["pymongo.asynchronous"]),
        ("from bson import ObjectId", ["bson"]),
        ("import openai, deepgram", ["openai", "deepgram"]),
        ("from sarvamai import AsyncSarvamAI", ["sarvamai"]),
        ("from voice_agent.stt_adapters import x", ["voice_agent.stt_adapters"]),
        ("from ..persistence import repo", ["voice_agent.persistence"]),
        ("from . import models", []),
        ("from voice_agent.contracts import Event", []),
        ("import pydantic\nfrom typing import Protocol", []),
    ],
)
def test_scanner_detects_violations(source: str, expected: list[str]) -> None:
    assert _violations(source, "voice_agent.domain") == expected


def test_package_resolution_for_init_and_module() -> None:
    init = PACKAGE_ROOT / "domain" / "__init__.py"
    module = PACKAGE_ROOT / "domain" / "session.py"

    assert _package_of(init) == "voice_agent.domain"
    assert _package_of(module) == "voice_agent.domain"
