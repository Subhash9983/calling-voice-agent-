"""Control-API route catalogue, envelope, and realtime boundary contract (docs/04 §17-§24).

- the served routes are exactly the approved docs/04 §23 catalogue;
- no route streams audio (no WebSocket/SSE) and ``control_api`` imports no
  realtime pipeline, provider adapter, provider SDK, or database driver;
- every request model forbids unknown fields in the generated OpenAPI.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute, APIWebSocketRoute

import voice_agent
from voice_agent.control_api.app import REQUEST_MODELS, create_app
from voice_agent.control_api.errors import HTTP_STATUS, ErrorCode
from voice_agent.control_api.routes import diagnostics, health, sessions
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID

PACKAGE_ROOT = Path(next(iter(voice_agent.__path__)))
CONTROL_API = PACKAGE_ROOT / "control_api"

APPROVED_CATALOGUE: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/api/v1/agent-configs"),
        ("GET", "/api/v1/agent-configs/{agent_config_id}"),
        ("POST", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions/{session_id}"),
        ("POST", "/api/v1/sessions/{session_id}/join-token"),
        ("POST", "/api/v1/sessions/{session_id}/end"),
        ("GET", "/api/v1/sessions/{session_id}/turns"),
        ("GET", "/api/v1/sessions/{session_id}/turns/{turn_id}"),
        ("GET", "/api/v1/sessions/{session_id}/events"),
        ("GET", "/api/v1/sessions/{session_id}/operations"),
        ("GET", "/api/v1/sessions/{session_id}/errors"),
        ("GET", "/api/v1/sessions/{session_id}/costs"),
        ("POST", "/api/v1/sessions/{session_id}/feedback"),
        ("POST", "/api/v1/sessions/{session_id}/consents"),
        ("GET", "/api/v1/sessions/{session_id}/consents/status"),
        ("POST", "/api/v1/consent-chains/{consent_chain_id}/revoke"),
    }
)
DOCUMENTATION_ROUTES = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect"})

# Realtime/audio/provider work never runs in the control API (docs/03 §5, docs/04 §1).
FORBIDDEN_IN_CONTROL_API: frozenset[str] = frozenset(
    {
        "voice_agent.agent_worker",
        "voice_agent.orchestration",
        "voice_agent.speech_activity",
        "voice_agent.turn_management",
        "voice_agent.stt_adapters",
        "voice_agent.conversation_adapters",
        "voice_agent.tts_adapters",
        "livekit",
        "openai",
        "deepgram",
        "sarvamai",
        "onnxruntime",
        "pymongo",
        "bson",
        "motor",
    }
)


ROUTERS = (health.router, sessions.router, diagnostics.router)


def _router_routes() -> list[object]:
    return [route for router in ROUTERS for route in router.routes]


def test_served_routes_are_exactly_the_approved_catalogue() -> None:
    app = create_app(environ={"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID})
    declared = {
        (method, route.path)
        for route in _router_routes()
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    documented = {
        (method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item
    }
    extra = {getattr(r, "path", None) for r in app.routes if hasattr(r, "path")}

    assert declared == APPROVED_CATALOGUE
    assert documented == APPROVED_CATALOGUE
    assert extra <= DOCUMENTATION_ROUTES


def test_no_websocket_or_streaming_routes() -> None:
    routes = _router_routes()

    assert routes
    assert not [route for route in routes if isinstance(route, APIWebSocketRoute)]
    for route in routes:
        assert isinstance(route, APIRoute)
        response_class = route.response_class
        # Default JSON responses only: no StreamingResponse/SSE/file responses.
        assert isinstance(response_class, DefaultPlaceholder), route.path
        assert inspect.iscoroutinefunction(route.endpoint), route.path


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize(
    "path", sorted(CONTROL_API.rglob("*.py")), ids=lambda p: p.relative_to(PACKAGE_ROOT).as_posix()
)
def test_control_api_imports_no_realtime_or_provider_code(path: Path) -> None:
    reached = [
        name
        for name in _imports(path)
        if any(name == root or name.startswith(root + ".") for root in FORBIDDEN_IN_CONTROL_API)
    ]

    assert reached == []


def test_control_api_modules_have_no_relative_imports() -> None:
    for path in CONTROL_API.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
        assert relative == [], path


def test_error_code_allowlist_matches_the_contract_table() -> None:
    expected = {
        "INVALID_REQUEST": 400,
        "AUTHENTICATION_REQUIRED": 401,
        "ACCESS_FORBIDDEN": 403,
        "RESOURCE_NOT_FOUND": 404,
        "INVALID_STATE": 409,
        "REVISION_CONFLICT": 409,
        "IDEMPOTENCY_CONFLICT": 409,
        "PAYLOAD_TOO_LARGE": 413,
        "VALIDATION_FAILED": 422,
        "CONSENT_REQUIRED": 422,
        "RATE_LIMITED": 429,
        "PROVIDER_UNAVAILABLE": 502,
        "DEPENDENCY_UNAVAILABLE": 503,
        "INTERNAL_ERROR": 500,
    }

    assert {code.value: status for code, status in HTTP_STATUS.items()} == expected
    assert set(ErrorCode) == set(HTTP_STATUS)


def _resolve(schema: dict[str, object], document: dict[str, object]) -> dict[str, object]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        components = document["components"]
        assert isinstance(components, dict)
        resolved = components["schemas"][name]
        assert isinstance(resolved, dict)
        return resolved
    return schema


def test_openapi_request_bodies_forbid_unknown_fields() -> None:
    app = create_app(environ={"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID})
    document = app.openapi()
    schemas = document["components"]["schemas"]
    body_models = [m for m in REQUEST_MODELS if m.__name__ in schemas]

    assert body_models
    for model in body_models:
        assert schemas[model.__name__].get("additionalProperties") is False, model.__name__
    for path_item in document["paths"].values():
        for operation in path_item.values():
            body = operation.get("requestBody")
            if body is None:
                continue
            schema = _resolve(body["content"]["application/json"]["schema"], document)
            assert schema.get("additionalProperties") is False


def test_openapi_documents_the_error_envelope() -> None:
    app = create_app(environ={"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID})
    document = app.openapi()

    envelope = document["components"]["schemas"]["ErrorEnvelope"]
    assert set(envelope["properties"]) == {"error", "request_id"}
    create = document["paths"]["/api/v1/sessions"]["post"]["responses"]
    assert {"201", "409", "422", "503"} <= set(create)
