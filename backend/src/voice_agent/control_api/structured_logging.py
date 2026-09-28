"""Safe structured JSON logging for the control API (docs/04 §20; docs/12 §13).

Log records carry an event name, the request/correlation/session IDs from
the request context, and only allowlisted scalar fields. Headers, bodies,
query strings, tokens, settings, and exception messages/tracebacks are never
logged; the message is additionally passed through defensive redaction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from voice_agent.control_api.request_context import current_context
from voice_agent.security.redaction import redact_text

LOGGER_NAME: Final = "voice_agent.control_api"
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "method",
        "route",
        "status_code",
        "duration_ms",
        "error_code",
        "error_type",
        "ready",
        "reasons",
        "component",
        "host",
        "port",
        "operation",
    }
)
_FIELDS_ATTRIBUTE: Final = "safe_fields"
Scalar = str | int | float | bool | None


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _safe_value(value: object) -> Scalar | list[str]:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [redact_text(str(item))[:64] for item in value[:20]]
    return redact_text(str(value))[:200]


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Emit one structured event; unknown field names are dropped."""
    safe = {key: _safe_value(value) for key, value in fields.items() if key in ALLOWED_FIELDS}
    logger.log(level, event, extra={_FIELDS_ATTRIBUTE: safe})


class SafeJsonFormatter(logging.Formatter):
    """Render records as one JSON object; never renders ``exc_info`` or ``args``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(str(record.msg))[:200],
        }
        context = current_context()
        if context is not None:
            payload["request_id"] = context.request_id
            payload["correlation_id"] = context.correlation_id
            if context.session_id is not None:
                payload["session_id"] = context.session_id
        fields = getattr(record, _FIELDS_ATTRIBUTE, None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(level: str) -> None:
    """Install the safe formatter on the control-API logger (idempotent)."""
    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(h.formatter, SafeJsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(SafeJsonFormatter())
        logger.addHandler(handler)
