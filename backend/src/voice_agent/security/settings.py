"""Pydantic Settings v2 bootstrap settings (docs/12 §3, §5, §6; Decisions 036, 058, 067).

``BootstrapSettings`` reads no ambient source by itself: the only settings
source is explicit constructor input assembled by ``config_loader`` in the
approved precedence order. Secrets are ``SecretStr`` so reprs and JSON
serialization mask them, and validation errors never include input values.

Secret values are stored as provided; whether an *enabled* credential is
missing, blank, placeholder, or malformed is decided by readiness, so a
disabled challenger key can never break the baseline (docs/12 §5).
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from voice_agent.contracts.base import CanonicalId

SECRETS_FILE_VARIABLE: Final = "VOICE_AGENT_SECRETS_FILE"
APPROVED_API_HOST: Final = "127.0.0.1"
APPROVED_PUBLIC_ORIGINS: frozenset[str] = frozenset({"http://127.0.0.1:5173"})
APPROVED_DATABASE: Final = "voice_agent_rnd"
MAX_PORT = 65_535
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_PORT_DIGITS = re.compile(r"^[0-9]{1,5}$")

# Credential settings (docs/12 §5). XAI/ELEVENLABS are optional challengers.
SECRET_ALIASES: tuple[str, ...] = (
    "MONGODB_URI",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "SARVAM_API_KEY",
    "XAI_API_KEY",
    "ELEVENLABS_API_KEY",
)


class AppEnvironment(StrEnum):
    """Phase 0 accepts only these; ``production`` is rejected at startup."""

    DEVELOPMENT = "development"
    RD = "rd"


def _parse_port(value: Any) -> Any:
    if isinstance(value, str):
        if not _PORT_DIGITS.match(value):
            raise ValueError("port must be a plain decimal integer")
        return int(value)
    return value


def _validate_origin(value: str) -> str:
    if value not in APPROVED_PUBLIC_ORIGINS:
        raise ValueError("public origin must be an exact approved origin")
    return value


def _validate_agent_name(value: str) -> str:
    if not _AGENT_NAME.match(value):
        raise ValueError("agent name must be a lowercase label")
    return value


def _validate_livekit_url(value: str) -> str:
    parts = urlsplit(value)
    has_extras = parts.query or parts.fragment or parts.path not in ("", "/")
    if parts.scheme != "wss" or not parts.hostname or parts.username or has_extras:
        raise ValueError("LiveKit URL must be a wss:// host without credentials or query")
    return value


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


Port = Annotated[int, BeforeValidator(_parse_port), Field(strict=True, ge=1, le=MAX_PORT)]
PublicOrigin = Annotated[str, AfterValidator(_validate_origin)]
AgentName = Annotated[str, AfterValidator(_validate_agent_name)]
LiveKitUrl = Annotated[str, AfterValidator(_validate_livekit_url)]
OptionalConfigId = Annotated[CanonicalId | None, BeforeValidator(_blank_to_none)]


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        case_sensitive=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    app_env: AppEnvironment = Field(default=AppEnvironment.DEVELOPMENT, alias="APP_ENV")
    app_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="APP_LOG_LEVEL"
    )
    app_api_host: Literal["127.0.0.1"] = Field(default=APPROVED_API_HOST, alias="APP_API_HOST")
    app_api_port: Port = Field(default=8000, alias="APP_API_PORT")
    app_public_origin: PublicOrigin = Field(
        default="http://127.0.0.1:5173", alias="APP_PUBLIC_ORIGIN"
    )
    app_agent_name: AgentName = Field(default="phase0-voice-agent", alias="APP_AGENT_NAME")
    app_default_agent_config_id: OptionalConfigId = Field(
        default=None, alias="APP_DEFAULT_AGENT_CONFIG_ID"
    )
    mongodb_database: Literal["voice_agent_rnd"] = Field(
        default=APPROVED_DATABASE, alias="MONGODB_DATABASE"
    )
    # Infrastructure topology (docs/12 §5): server-side only, never repr/serialized.
    livekit_url: LiveKitUrl | None = Field(
        default=None, alias="LIVEKIT_URL", repr=False, exclude=True
    )
    # The path is host filesystem detail: never repr'd or serialized (docs/12 §11, §13).
    voice_agent_secrets_file: Path | None = Field(
        default=None, alias="VOICE_AGENT_SECRETS_FILE", repr=False, exclude=True
    )

    mongodb_uri: SecretStr | None = Field(default=None, alias="MONGODB_URI")
    livekit_api_key: SecretStr | None = Field(default=None, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr | None = Field(default=None, alias="LIVEKIT_API_SECRET")
    deepgram_api_key: SecretStr | None = Field(default=None, alias="DEEPGRAM_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    sarvam_api_key: SecretStr | None = Field(default=None, alias="SARVAM_API_KEY")
    xai_api_key: SecretStr | None = Field(default=None, alias="XAI_API_KEY")
    elevenlabs_api_key: SecretStr | None = Field(default=None, alias="ELEVENLABS_API_KEY")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence is applied explicitly by ``config_loader``; no hidden
        # environment, dotenv, or secrets-directory reads happen here.
        return (init_settings,)

    def secret(self, alias: str) -> SecretStr | None:
        """Look up one credential setting by its approved environment name."""
        if alias not in SECRET_ALIASES:
            raise KeyError("unknown credential setting")
        value = getattr(self, alias.lower())
        return value if isinstance(value, SecretStr) else None


def setting_aliases() -> frozenset[str]:
    """Every accepted setting name (the explicit alias of each field)."""
    return frozenset(
        str(field.alias) for field in BootstrapSettings.model_fields.values() if field.alias
    )


SETTING_ALIASES: frozenset[str] = setting_aliases()
NON_SECRET_ALIASES: frozenset[str] = SETTING_ALIASES - frozenset(SECRET_ALIASES)
