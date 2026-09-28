"""Control-API process entry point (docs/14 §21; docs/12 §6, §12).

``python -m voice_agent.control_api [--persistence in_memory|mongodb]``

Startup order: load and validate bootstrap settings (fail safely with codes
only on invalid configuration), refuse any binding other than
``127.0.0.1:<port>``, then serve with uvicorn access logging disabled
(access logs would record raw query strings). Readiness may still be false
after startup; liveness stays independent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import uvicorn

from voice_agent.control_api.access import AccessGuardError, ensure_loopback_bind
from voice_agent.control_api.app import create_app
from voice_agent.control_api.structured_logging import configure_logging
from voice_agent.security.config_errors import ConfigurationError
from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.readiness import PersistenceMode

Serve = Callable[..., Any]
EXIT_OK = 0
EXIT_REFUSED = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Phase 0 control API.")
    parser.add_argument(
        "--persistence",
        choices=[mode.value for mode in PersistenceMode],
        default=PersistenceMode.IN_MEMORY.value,
    )
    return parser


def _refuse(payload: Mapping[str, object]) -> int:
    sys.stderr.write(json.dumps({"started": False, **payload}, sort_keys=True) + "\n")
    return EXIT_REFUSED


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    serve: Serve = uvicorn.run,
) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_bootstrap_configuration(environ)
    except ConfigurationError as exc:
        return _refuse({"diagnostics": [item.to_safe_dict() for item in exc.diagnostics]})
    settings = loaded.settings
    try:
        ensure_loopback_bind(settings.app_api_host, settings.app_api_port)
    except AccessGuardError:
        return _refuse({"reason": "binding_not_allowed"})
    configure_logging(settings.app_log_level)
    app = create_app(environ=environ, persistence=PersistenceMode(args.persistence))
    serve(
        app,
        host=settings.app_api_host,
        port=settings.app_api_port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_level=settings.app_log_level.lower(),
    )
    return EXIT_OK
