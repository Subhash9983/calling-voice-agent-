"""Redacted configuration diagnostics (docs/12 §12, §13).

The only permitted view of loaded configuration for logs and evidence. Safe
non-secret settings appear by value; the LiveKit URL and secret-file path
appear only as ``configured`` flags; credentials appear only as availability,
normalized status, and source class. Never a settings or environment dump.
"""

from __future__ import annotations

from voice_agent.security.config_loader import LoadedConfiguration
from voice_agent.security.credentials import CredentialStatus, classify_credential
from voice_agent.security.settings import SECRET_ALIASES

NOT_CONFIGURED = "not_configured"


def _credentials(loaded: LoadedConfiguration) -> dict[str, dict[str, str | bool]]:
    evidence: dict[str, dict[str, str | bool]] = {}
    for name in SECRET_ALIASES:
        status = classify_credential(name, loaded.settings.secret(name))
        source = loaded.provenance.get(name)
        evidence[name] = {
            "credential_available": status is CredentialStatus.AVAILABLE,
            "credential_status": status.value,
            "credential_source": (
                NOT_CONFIGURED if status is CredentialStatus.MISSING or source is None else source
            ),
        }
    return evidence


def redacted_configuration_diagnostics(loaded: LoadedConfiguration) -> dict[str, object]:
    settings = loaded.settings
    return {
        "app_env": settings.app_env.value,
        "app_log_level": settings.app_log_level,
        "app_api_host": settings.app_api_host,
        "app_api_port": settings.app_api_port,
        "app_public_origin": settings.app_public_origin,
        "app_agent_name": settings.app_agent_name,
        "app_default_agent_config_id": settings.app_default_agent_config_id,
        "mongodb_database": settings.mongodb_database,
        "livekit_url_configured": settings.livekit_url is not None,
        "secrets_file_configured": settings.voice_agent_secrets_file is not None,
        "sources": {name: source.value for name, source in sorted(loaded.provenance.items())},
        "credentials": _credentials(loaded),
        "warnings": [item.to_safe_dict() for item in loaded.diagnostics],
    }
