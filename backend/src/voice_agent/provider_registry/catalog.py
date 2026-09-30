"""Approved agent-configuration catalogue for control-plane reads (docs/04 §5; docs/12 §7).

Each candidate document passes the same ``check_agent_config`` gate used at
startup; only active, checksum-valid, environment-matching configurations
with approved adapters are selectable. Until MongoDB lands (WP5) the
catalogue is built from the built-in configurations.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.provider_registry.approved import check_agent_config
from voice_agent.provider_registry.media_check_config import media_check_agent_config_document
from voice_agent.provider_registry.mock_config import mock_agent_config_document
from voice_agent.provider_registry.stt_check_config import stt_check_agent_config_document
from voice_agent.security.settings import AppEnvironment


def builtin_agent_config_documents() -> tuple[dict[str, Any], ...]:
    return (
        mock_agent_config_document(),
        media_check_agent_config_document(),
        stt_check_agent_config_document(),
    )


def _approved(document: Mapping[str, Any], app_env: AppEnvironment) -> AgentConfig | None:
    config_id = document.get("agent_config_id")
    expected = config_id if isinstance(config_id, str) else None
    check = check_agent_config(document, expected_config_id=expected, app_env=app_env)
    return check.config if check.config is not None and not check.issues else None


class ApprovedAgentConfigCatalog:
    def __init__(self, documents: Iterable[Mapping[str, Any]], *, app_env: AppEnvironment) -> None:
        approved = (_approved(document, app_env) for document in documents)
        self._configs: dict[str, AgentConfig] = {
            config.agent_config_id: config for config in approved if config is not None
        }

    async def list_active(self, environment: str) -> Sequence[AgentConfig]:
        matches = [c for c in self._configs.values() if c.environment.value == environment]
        return sorted(matches, key=lambda config: (config.name, config.agent_config_id))

    async def get_active(self, agent_config_id: str) -> AgentConfig | None:
        return self.approved(agent_config_id)

    def approved(self, agent_config_id: str) -> AgentConfig | None:
        """Synchronous lookup for startup wiring."""
        return self._configs.get(agent_config_id)
