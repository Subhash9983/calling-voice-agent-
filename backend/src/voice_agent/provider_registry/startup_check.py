"""Fail-safe startup configuration check for the API and worker (docs/14 §21 step 1).

``python -m voice_agent.provider_registry.startup_check --role agent_worker
--persistence in_memory`` validates bootstrap settings and the default agent
configuration, prints a safe JSON readiness report, and exits ``0`` when
ready or ``1`` otherwise. It makes no network or provider call and never
prints a value, path, or traceback. Until MongoDB lands (WP5) only built-in
configurations can be selected.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voice_agent.provider_registry.approved import check_agent_config
from voice_agent.provider_registry.media_check_config import (
    MEDIA_CHECK_AGENT_CONFIG_ID,
    media_check_agent_config_document,
)
from voice_agent.provider_registry.mock_config import (
    MOCK_AGENT_CONFIG_ID,
    mock_agent_config_document,
)
from voice_agent.provider_registry.stt_check_config import (
    STT_CHECK_AGENT_CONFIG_ID,
    stt_check_agent_config_document,
)
from voice_agent.security.config_errors import ConfigurationError
from voice_agent.security.config_loader import LoadedConfiguration, load_bootstrap_configuration
from voice_agent.security.diagnostics import redacted_configuration_diagnostics
from voice_agent.security.readiness import (
    AgentConfigCheck,
    PersistenceMode,
    ProcessRole,
    ReadinessReport,
    evaluate_readiness,
)

ConfigLookup = Callable[[str], Mapping[str, Any] | None]
_LOGGER = logging.getLogger(__name__)


def builtin_config_lookup(config_id: str) -> Mapping[str, Any] | None:
    if config_id == MOCK_AGENT_CONFIG_ID:
        return mock_agent_config_document()
    if config_id == MEDIA_CHECK_AGENT_CONFIG_ID:
        return media_check_agent_config_document()
    if config_id == STT_CHECK_AGENT_CONFIG_ID:
        return stt_check_agent_config_document()
    return None


@dataclass(frozen=True, slots=True)
class StartupOutcome:
    report: ReadinessReport
    loaded: LoadedConfiguration | None

    def to_safe_dict(self) -> dict[str, object]:
        data = self.report.to_safe_dict()
        if self.loaded is not None:
            data["configuration"] = redacted_configuration_diagnostics(self.loaded)
        return data


def run_startup_check(
    role: ProcessRole,
    *,
    persistence: PersistenceMode,
    environ: Mapping[str, str] | None = None,
    lookup: ConfigLookup = builtin_config_lookup,
    safe_configuration: Mapping[str, str] | None = None,
    protected_roots: Sequence[Path] | None = None,
) -> StartupOutcome:
    loaded: LoadedConfiguration | ConfigurationError
    try:
        loaded = load_bootstrap_configuration(
            environ, safe_configuration=safe_configuration, protected_roots=protected_roots
        )
    except ConfigurationError as exc:
        loaded = exc
    check: AgentConfigCheck | None = None
    if isinstance(loaded, LoadedConfiguration):
        expected = loaded.settings.app_default_agent_config_id
        document = lookup(expected) if expected is not None else None
        check = check_agent_config(
            document, expected_config_id=expected, app_env=loaded.settings.app_env
        )
    report = evaluate_readiness(loaded, check, role=role, persistence=persistence)
    success = loaded if isinstance(loaded, LoadedConfiguration) else None
    outcome = StartupOutcome(report=report, loaded=success)
    _LOGGER.info("startup readiness %s", json.dumps(report.to_safe_dict(), sort_keys=True))
    return outcome


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate configuration without secrets output.")
    parser.add_argument("--role", choices=[r.value for r in ProcessRole], required=True)
    parser.add_argument(
        "--persistence",
        choices=[m.value for m in PersistenceMode],
        default=PersistenceMode.MONGODB.value,
    )
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    outcome = run_startup_check(
        ProcessRole(args.role), persistence=PersistenceMode(args.persistence), environ=environ
    )
    sys.stdout.write(json.dumps(outcome.to_safe_dict(), sort_keys=True) + "\n")
    return 0 if outcome.report.ready else 1


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess test
    raise SystemExit(main())
