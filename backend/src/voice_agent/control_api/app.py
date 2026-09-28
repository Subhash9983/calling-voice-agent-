"""FastAPI control-API application factory (docs/03 §5; docs/04; docs/14 §10).

``create_app`` runs the fail-safe WP3 startup check (settings, secret-file,
default agent configuration) synchronously, composes control-plane readiness,
and wires the routes and middleware:

``RequestContextMiddleware`` (request/correlation IDs, body bound, error
boundary) -> ``AccessGuardMiddleware`` (loopback peer, Host, Origin) ->
exact-origin CORS -> routes. Invalid configuration never prevents liveness;
it makes readiness and every ``/api/v1`` route report the not-ready state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from voice_agent.control_api.access import (
    DEFAULT_API_PORT,
    AccessGuardMiddleware,
    access_policy,
)
from voice_agent.control_api.error_handlers import install_exception_handlers
from voice_agent.control_api.middleware import RequestContextMiddleware
from voice_agent.control_api.routes import diagnostics, health, sessions
from voice_agent.control_api.runtime import ControlPlaneRuntime, RuntimeOverrides, build_runtime
from voice_agent.control_api.schemas.diagnostics import (
    ErrorListParams,
    EventListParams,
    OperationListParams,
    TurnListParams,
)
from voice_agent.control_api.schemas.engagement import (
    ConsentRequest,
    ConsentRevokeRequest,
    ConsentStatusParams,
    FeedbackRequest,
)
from voice_agent.control_api.schemas.sessions import (
    AgentConfigListParams,
    EndSessionRequest,
    JoinTokenRequest,
    SessionCreateRequest,
    SessionListParams,
)
from voice_agent.control_api.structured_logging import get_logger, log_event
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.startup_check import run_startup_check
from voice_agent.security.readiness import PersistenceMode, ProcessRole
from voice_agent.security.settings import AppEnvironment

API_TITLE = "Voice agent control API"
API_VERSION = "0.4.0"
REQUEST_MODELS = (
    AgentConfigListParams,
    SessionCreateRequest,
    SessionListParams,
    JoinTokenRequest,
    EndSessionRequest,
    TurnListParams,
    EventListParams,
    OperationListParams,
    ErrorListParams,
    FeedbackRequest,
    ConsentRequest,
    ConsentStatusParams,
    ConsentRevokeRequest,
)


def _lookup(documents: Sequence[Mapping[str, Any]]) -> Any:
    by_id = {str(doc.get("agent_config_id")): doc for doc in documents}
    return by_id.get


def _runtime_of(app: FastAPI) -> ControlPlaneRuntime:
    runtime = app.state.runtime
    if not isinstance(runtime, ControlPlaneRuntime):  # pragma: no cover - wiring invariant
        raise TypeError("control-plane runtime is not configured")
    return runtime


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    report = _runtime_of(app).startup_report
    log_event(
        get_logger(),
        logging.INFO,
        "control_api.started",
        ready=report.ready,
        reasons=[reason.value for reason in report.reasons],
    )
    yield
    log_event(get_logger(), logging.INFO, "control_api.stopped")


def create_app(
    *,
    environ: Mapping[str, str] | None = None,
    persistence: PersistenceMode = PersistenceMode.IN_MEMORY,
    safe_configuration: Mapping[str, str] | None = None,
    protected_roots: Sequence[Path] | None = None,
    overrides: RuntimeOverrides | None = None,
) -> FastAPI:
    options = overrides or RuntimeOverrides()
    documents = tuple(options.config_documents or builtin_agent_config_documents())
    outcome = run_startup_check(
        ProcessRole.CONTROL_API,
        persistence=persistence,
        environ=environ,
        lookup=_lookup(documents),
        safe_configuration=safe_configuration,
        protected_roots=protected_roots,
    )
    runtime = build_runtime(
        outcome, documents=documents, persistence=persistence, overrides=options
    )
    settings = runtime.settings
    port = settings.app_api_port if settings is not None else DEFAULT_API_PORT
    origin = settings.app_public_origin if settings is not None else None
    local_docs = settings is not None and settings.app_env is AppEnvironment.DEVELOPMENT
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=_lifespan,
        docs_url="/docs" if local_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if local_docs else None,
    )
    app.state.runtime = runtime
    install_exception_handlers(app, REQUEST_MODELS)
    for router in (health.router, sessions.router, diagnostics.router):
        app.include_router(router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin] if origin is not None else [],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
        allow_credentials=False,
        expose_headers=["x-request-id"],
        max_age=600,
    )
    app.add_middleware(AccessGuardMiddleware, policy=access_policy(port, origin))
    app.add_middleware(RequestContextMiddleware)
    return app
