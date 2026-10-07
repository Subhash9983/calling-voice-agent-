"""Pydantic Settings v2 bootstrap settings (docs/12 §3, §5, §6; Decisions 036, 058, 067).

``BootstrapSettings`` reads no ambient source by itself: the only settings
source is explicit constructor input assembled by ``config_loader`` in the
approved precedence order. Secrets are ``SecretStr`` so reprs and JSON
serialization mask them, and validation errors never include input values.

Secret values are stored as provided; whether an *enabled* credential is
missing, blank, placeholder, or malformed is decided by readiness, so a
disabled challenger key can never break the baseline (docs/12 §5).

Deployment mode (Decision 070, limited-sharing remote deployment exception):
``APP_DEPLOYMENT_MODE`` defaults to ``local``, in which every setting below
behaves exactly as before Decision 070 (``127.0.0.1`` bind only, the single
approved local origin, no public API host). Only the explicit value
``remote_limited_sharing`` relaxes, narrowly:

- ``APP_API_HOST`` may additionally be ``0.0.0.0`` (Render binds all
  interfaces); any other host is still rejected;
- ``APP_PUBLIC_ORIGIN`` must be one exact, well-formed ``https://`` origin
  (lowercase host, optional port, no path/query/fragment/credentials, no
  wildcard); in remote mode it *replaces* the local origin (CORS and the
  ``Origin`` check stay exact-match);
- ``APP_API_PUBLIC_HOST`` (required, remote only) is the backend's own public
  hostname, the only accepted ``Host`` header behind the hosting proxy;
- ``APP_DAILY_SPEND_CAP_INR`` (default and maximum INR 200.00) is the hard
  daily cap enforced at session creation; it is ignored in local mode.

No authentication, account, or session-ownership model is introduced.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from voice_agent.contracts.base import CanonicalId

SECRETS_FILE_VARIABLE: Final = "VOICE_AGENT_SECRETS_FILE"
APPROVED_API_HOST: Final = "127.0.0.1"
# Decision 070: bind-all is accepted only in ``remote_limited_sharing`` mode.
REMOTE_BIND_HOST: Final = "0.0.0.0"  # noqa: S104 - gated by the explicit deployment mode
APPROVED_PUBLIC_ORIGINS: frozenset[str] = frozenset({"http://127.0.0.1:5173"})
APPROVED_DATABASE: Final = "voice_agent_rnd"
# Decision 070: hard daily spend cap; the setting may lower it, never raise it.
DEFAULT_DAILY_SPEND_CAP_INR: Final = Decimal("200.00")
MAX_PORT = 65_535
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_PORT_DIGITS = re.compile(r"^[0-9]{1,5}$")
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# A public DNS name: at least two lowercase labels, the last alphabetic.
_PUBLIC_HOSTNAME = re.compile(rf"(?:{_DNS_LABEL}\.)+[a-z]{{2,63}}")
_HTTPS_ORIGIN = re.compile(rf"https://((?:{_DNS_LABEL}\.)+[a-z]{{2,63}})(?::([0-9]{{1,5}}))?")

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


class DeploymentMode(StrEnum):
    """``local`` is the Phase 0 default; the other value is Decision 070 only."""

    LOCAL = "local"
    REMOTE_LIMITED_SHARING = "remote_limited_sharing"


def _parse_port(value: Any) -> Any:
    if isinstance(value, str):
        if not _PORT_DIGITS.match(value):
            raise ValueError("port must be a plain decimal integer")
        return int(value)
    return value


def _is_remote(info: ValidationInfo) -> bool:
    # A mode that failed its own validation is absent here: treat it as local.
    return info.data.get("app_deployment_mode") is DeploymentMode.REMOTE_LIMITED_SHARING


def is_https_origin(value: str) -> bool:
    """One exact ``https://host[:port]`` origin: lowercase DNS host, nothing else."""
    match = _HTTPS_ORIGIN.fullmatch(value)
    if match is None:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port) <= MAX_PORT


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
    # Declared before every mode-dependent field so their validators can see it.
    app_deployment_mode: DeploymentMode = Field(
        default=DeploymentMode.LOCAL, alias="APP_DEPLOYMENT_MODE"
    )
    app_api_host: Literal["127.0.0.1", "0.0.0.0"] = Field(  # noqa: S104 - mode-gated below
        default=APPROVED_API_HOST, alias="APP_API_HOST"
    )
    app_api_port: Port = Field(default=8000, alias="APP_API_PORT")
    app_public_origin: str = Field(default="http://127.0.0.1:5173", alias="APP_PUBLIC_ORIGIN")
    app_api_public_host: str | None = Field(default=None, alias="APP_API_PUBLIC_HOST")
    app_daily_spend_cap_inr: Decimal = Field(
        default=DEFAULT_DAILY_SPEND_CAP_INR,
        alias="APP_DAILY_SPEND_CAP_INR",
        gt=0,
        le=DEFAULT_DAILY_SPEND_CAP_INR,
        decimal_places=2,
        allow_inf_nan=False,
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

    @field_validator("app_api_host")
    @classmethod
    def _approved_bind_host(cls, value: str, info: ValidationInfo) -> str:
        if value != APPROVED_API_HOST and not _is_remote(info):
            raise ValueError("only remote_limited_sharing mode may bind all interfaces")
        return value

    @field_validator("app_public_origin")
    @classmethod
    def _approved_origin(cls, value: str, info: ValidationInfo) -> str:
        if _is_remote(info):
            if not is_https_origin(value):
                raise ValueError("remote public origin must be one exact https origin")
        elif value not in APPROVED_PUBLIC_ORIGINS:
            raise ValueError("public origin must be an exact approved origin")
        return value

    @field_validator("app_api_public_host")
    @classmethod
    def _approved_public_host(cls, value: str | None, info: ValidationInfo) -> str | None:
        if not _is_remote(info):
            if value is not None:
                raise ValueError("a public API host is accepted only in remote mode")
            return None
        if value is None or not _PUBLIC_HOSTNAME.fullmatch(value):
            raise ValueError("remote mode needs the exact public API hostname")
        return value

    @property
    def is_remote_limited_sharing(self) -> bool:
        return self.app_deployment_mode is DeploymentMode.REMOTE_LIMITED_SHARING

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
