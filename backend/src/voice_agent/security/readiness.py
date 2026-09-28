"""Configuration readiness with normalized safe codes (docs/12 §12; docs/04 §4; docs/05 §2).

Readiness here covers configuration only: bootstrap settings, the selected
immutable agent configuration, and presence/shape of every credential an
enabled dependency needs. Network checks (MongoDB ping, LiveKit capability)
are added by later work packages on top of this report. Each component result
is ``(component, status, reason)`` with enum values only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.security.config_errors import ConfigReason, ConfigurationError
from voice_agent.security.config_loader import LoadedConfiguration
from voice_agent.security.credentials import (
    LIVEKIT_CREDENTIAL_NAMES,
    PROVIDER_CREDENTIAL_NAME,
    classify_credential,
)
from voice_agent.security.settings import AppEnvironment, BootstrapSettings

LIVEKIT_PROVIDER = "livekit"


class ReadinessComponent(StrEnum):
    SETTINGS = "settings"
    AGENT_CONFIG = "agent_config"
    PERSISTENCE = "persistence"
    TRANSPORT = "transport"
    STT = "stt"
    CONVERSATION_ENGINE = "conversation_engine"
    TTS = "tts"


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    DISABLED = "disabled"


class ProcessRole(StrEnum):
    CONTROL_API = "control_api"
    AGENT_WORKER = "agent_worker"


class PersistenceMode(StrEnum):
    MONGODB = "mongodb"
    IN_MEMORY = "in_memory"


PROVIDER_COMPONENTS: tuple[ReadinessComponent, ...] = (
    ReadinessComponent.STT,
    ReadinessComponent.CONVERSATION_ENGINE,
    ReadinessComponent.TTS,
)
_SECTION_OF: Mapping[ReadinessComponent, str] = MappingProxyType(
    {
        ReadinessComponent.TRANSPORT: "transport",
        ReadinessComponent.STT: "stt",
        ReadinessComponent.CONVERSATION_ENGINE: "conversation_engine",
        ReadinessComponent.TTS: "tts",
    }
)


@dataclass(frozen=True, slots=True)
class ComponentReadiness:
    component: ReadinessComponent
    status: ReadinessStatus
    reason: ConfigReason

    def to_safe_dict(self) -> dict[str, str]:
        return {"component": self.component, "status": self.status, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class AgentConfigCheck:
    """Outcome of selecting and approving the default agent configuration."""

    config: AgentConfig | None
    issues: Mapping[ReadinessComponent, ConfigReason] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    role: ProcessRole
    components: tuple[ComponentReadiness, ...]

    @property
    def ready(self) -> bool:
        return all(item.status is not ReadinessStatus.NOT_READY for item in self.components)

    @property
    def reasons(self) -> tuple[ConfigReason, ...]:
        return tuple(
            item.reason for item in self.components if item.status is ReadinessStatus.NOT_READY
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "ready": self.ready,
            "components": [item.to_safe_dict() for item in self.components],
        }


def _result(
    component: ReadinessComponent, reason: ConfigReason, *, disabled: bool = False
) -> ComponentReadiness:
    if disabled:
        return ComponentReadiness(component, ReadinessStatus.DISABLED, reason)
    ok = reason in (ConfigReason.OK, ConfigReason.MOCK_ADAPTER, ConfigReason.IN_MEMORY_PERSISTENCE)
    status = ReadinessStatus.READY if ok else ReadinessStatus.NOT_READY
    return ComponentReadiness(component, status, reason)


def _first_failure(settings: BootstrapSettings, names: tuple[str, ...]) -> ConfigReason:
    for name in names:
        status = classify_credential(name, settings.secret(name))
        if status.reason is not ConfigReason.OK:
            return status.reason
    return ConfigReason.OK


def _persistence(settings: BootstrapSettings, mode: PersistenceMode) -> ConfigReason:
    if mode is PersistenceMode.IN_MEMORY:
        if settings.app_env is AppEnvironment.DEVELOPMENT:
            return ConfigReason.IN_MEMORY_PERSISTENCE
        return ConfigReason.PERSISTENCE_MODE_NOT_ALLOWED
    return _first_failure(settings, ("MONGODB_URI",))


def _provider(settings: BootstrapSettings, provider: str) -> ConfigReason:
    if provider == LIVEKIT_PROVIDER:
        if settings.livekit_url is None:
            return ConfigReason.CONNECTION_URL_MISSING
        return _first_failure(settings, LIVEKIT_CREDENTIAL_NAMES)
    name = PROVIDER_CREDENTIAL_NAME.get(provider)
    return ConfigReason.MOCK_ADAPTER if name is None else _first_failure(settings, (name,))


def _adapter_component(
    component: ReadinessComponent,
    settings: BootstrapSettings,
    check: AgentConfigCheck,
    role: ProcessRole,
) -> ComponentReadiness:
    if role is ProcessRole.CONTROL_API and component in PROVIDER_COMPONENTS:
        return _result(component, ConfigReason.DISABLED, disabled=True)
    if component in check.issues:
        return _result(component, check.issues[component])
    if check.config is None:
        return _result(component, ConfigReason.NOT_EVALUATED)
    section = getattr(check.config, _SECTION_OF[component])
    return _result(component, _provider(settings, str(section.provider)))


def evaluate_readiness(
    loaded: LoadedConfiguration | ConfigurationError,
    check: AgentConfigCheck | None,
    *,
    role: ProcessRole,
    persistence: PersistenceMode,
) -> ReadinessReport:
    """Build a safe readiness report; never raises for configuration problems."""
    if isinstance(loaded, ConfigurationError):
        first = loaded.diagnostics[0].reason
        rest = tuple(
            _result(component, ConfigReason.NOT_EVALUATED)
            for component in ReadinessComponent
            if component is not ReadinessComponent.SETTINGS
        )
        return ReadinessReport(role, (_result(ReadinessComponent.SETTINGS, first), *rest))
    settings = loaded.settings
    selection = check if check is not None else AgentConfigCheck(config=None)
    agent_reason = selection.issues.get(ReadinessComponent.AGENT_CONFIG)
    if agent_reason is None and selection.config is None:
        agent_reason = ConfigReason.NOT_EVALUATED
    components = (
        _result(ReadinessComponent.SETTINGS, ConfigReason.OK),
        _result(ReadinessComponent.AGENT_CONFIG, agent_reason or ConfigReason.OK),
        _result(ReadinessComponent.PERSISTENCE, _persistence(settings, persistence)),
        *(
            _adapter_component(component, settings, selection, role)
            for component in (ReadinessComponent.TRANSPORT, *PROVIDER_COMPONENTS)
        ),
    )
    return ReadinessReport(role, components)
