"""Error sanitization, cursors, and safe structured logging (docs/04 §19-§20; docs/12 §13)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from voice_agent.control_api import structured_logging
from voice_agent.control_api.cursors import (
    decode_operation_cursor,
    decode_sequence_cursor,
    decode_session_cursor,
    encode_cursor,
    encode_operation_cursor,
    encode_session_cursor,
)
from voice_agent.control_api.errors import (
    MAX_FIELD_ERRORS,
    UNRECOGNIZED,
    ApiError,
    ErrorCode,
    field_errors_from_items,
    field_errors_from_validation_error,
    safe_location,
)
from voice_agent.control_api.request_context import (
    activate,
    bind_session,
    current_correlation_id,
    current_request_id,
    new_request_context,
)
from voice_agent.control_api.structured_logging import (
    SafeJsonFormatter,
    configure_logging,
    get_logger,
    log_event,
)

KNOWN = frozenset({"client_request_id", "reason"})
STAMP = datetime(2026, 9, 28, tzinfo=UTC)
SESSION = "00000000-0000-4000-8000-000000000001"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: int


def test_safe_location_keeps_only_known_names() -> None:
    assert safe_location(("body", "reason"), KNOWN) == "body.reason"
    assert safe_location(("body", "sk-secret-key"), KNOWN) == f"body.{UNRECOGNIZED}"
    assert safe_location(("body", "items", 3), KNOWN) == f"body.{UNRECOGNIZED}.3"
    assert safe_location(("reason",), KNOWN) == "reason"
    assert safe_location((), KNOWN) == UNRECOGNIZED


def test_field_errors_never_use_input_or_ctx() -> None:
    items = [
        {"loc": ("body", "reason"), "type": "enum", "input": "canary", "ctx": {"x": "canary"}},
        {"loc": ("body", "reason"), "type": "Not A Type!"},
    ]

    errors = field_errors_from_items(items, KNOWN)

    assert [e.model_dump() for e in errors] == [
        {"field": "body.reason", "code": "enum"},
        {"field": "body.reason", "code": "invalid"},
    ]


def test_field_errors_are_bounded() -> None:
    items = [{"loc": ("body", "reason"), "type": "missing"}] * (MAX_FIELD_ERRORS + 5)

    assert len(field_errors_from_items(items, KNOWN)) == MAX_FIELD_ERRORS


def test_validation_error_mapping_excludes_input() -> None:
    canary = "canary-value-that-must-not-leak"
    with pytest.raises(ValidationError) as excinfo:
        _Strict.model_validate({"reason": canary, canary: 1})

    errors = field_errors_from_validation_error(excinfo.value, KNOWN)

    assert canary not in json.dumps([e.model_dump() for e in errors])
    assert {"field": "reason", "code": "int_parsing"} in [e.model_dump() for e in errors]


def test_api_error_defaults_and_status() -> None:
    error = ApiError(ErrorCode.INVALID_STATE)

    assert error.status_code == 409
    assert error.body().message == "The resource state does not allow this operation."
    assert str(error) == "INVALID_STATE"


def test_cursor_round_trips() -> None:
    assert decode_sequence_cursor("turns", encode_cursor("turns", {"s": 7})) == 7
    assert decode_sequence_cursor("turns", None) == 0
    session = decode_session_cursor(encode_session_cursor(STAMP, SESSION))
    assert session is not None
    assert (session.created_at, session.session_id) == (STAMP, SESSION)
    operation = decode_operation_cursor(encode_operation_cursor(STAMP, SESSION))
    assert operation is not None
    assert operation.operation_id == SESSION
    assert decode_session_cursor(None) is None
    assert decode_operation_cursor(None) is None


@pytest.mark.parametrize(
    "cursor",
    [
        "%%%",
        encode_cursor("events", {"s": 1}),
        encode_cursor("turns", {"s": 0}),
        encode_cursor("turns", {"s": True}),
        encode_cursor("turns", {"s": "1"}),
        "W10",  # base64 of "[]"
    ],
)
def test_invalid_sequence_cursors(cursor: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        decode_sequence_cursor("turns", cursor)

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "values",
    [
        {"c": "not-a-date", "i": SESSION},
        {"c": "2026-09-28T00:00:00", "i": SESSION},
        {"c": STAMP.isoformat(), "i": "short"},
        {"c": 5, "i": SESSION},
    ],
)
def test_invalid_timestamp_cursors(values: dict[str, str | int]) -> None:
    with pytest.raises(ApiError):
        decode_session_cursor(encode_cursor("sessions", values))
    with pytest.raises(ApiError):
        decode_operation_cursor(encode_cursor("operations", values))


def test_request_context_generation_and_session_binding() -> None:
    context = new_request_context()
    activate(context)

    assert current_request_id() == context.request_id
    bind_session(SESSION, "session-correlation")
    assert current_correlation_id() == "session-correlation"


def test_formatter_emits_allowlisted_redacted_json(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=structured_logging.LOGGER_NAME)
    activate(new_request_context())

    log_event(
        get_logger(),
        logging.INFO,
        "request.completed",
        route="/x?token=Bearer abc.def.ghi",
        status_code=200,
        headers={"authorization": "Bearer secret"},
        reasons=["ok", "api_key=sk-abcdefghijklmnopqrstuvwx"],
    )

    record = caplog.records[-1]
    payload = json.loads(SafeJsonFormatter().format(record))
    assert payload["event"] == "request.completed"
    assert "headers" not in payload
    assert payload["status_code"] == 200
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(payload)
    assert "request_id" in payload


def test_formatter_never_renders_exception_text() -> None:
    try:
        raise RuntimeError("canary-exception-text")
    except RuntimeError:
        record = logging.LogRecord(
            "voice_agent.control_api", logging.ERROR, __file__, 1, "request.failed", None, None
        )
        import sys

        record.exc_info = sys.exc_info()

    assert "canary-exception-text" not in SafeJsonFormatter().format(record)


def test_configure_logging_is_idempotent() -> None:
    logger = get_logger()
    saved = (list(logger.handlers), logger.propagate, logger.level)
    try:
        configure_logging("WARNING")
        configure_logging("WARNING")
        formatters = [h for h in logger.handlers if isinstance(h.formatter, SafeJsonFormatter)]
        assert len(formatters) == 1
        assert logger.propagate is False
    finally:
        logger.handlers[:] = saved[0]
        logger.propagate = saved[1]
        logger.setLevel(saved[2])
