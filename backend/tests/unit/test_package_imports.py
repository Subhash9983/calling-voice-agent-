"""Every canonical backend subpackage (docs/14 §4) exists and imports cleanly."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import voice_agent

CANONICAL_SUBPACKAGES: tuple[str, ...] = (
    "control_api",
    "agent_worker",
    "orchestration",
    "speech_activity",
    "turn_management",
    "response_segmentation",
    "contracts",
    "domain",
    "ports",
    "provider_registry",
    "transport_adapters",
    "stt_adapters",
    "conversation_adapters",
    "tts_adapters",
    "persistence",
    "events_and_latency",
    "costing",
    "privacy_and_retention",
    "security",
    "maintenance",
    "evaluation",
)


def test_root_package_has_docstring() -> None:
    assert voice_agent.__doc__
    assert voice_agent.__doc__.strip()


@pytest.mark.parametrize("name", CANONICAL_SUBPACKAGES)
def test_subpackage_imports_with_docstring(name: str) -> None:
    module = importlib.import_module(f"voice_agent.{name}")

    assert hasattr(module, "__path__"), f"voice_agent.{name} must be a package"
    assert module.__doc__
    assert module.__doc__.strip()


def test_layout_matches_canonical_list_exactly() -> None:
    discovered = {info.name for info in pkgutil.iter_modules(voice_agent.__path__) if info.ispkg}

    assert discovered == set(CANONICAL_SUBPACKAGES)
