"""Feedback submission and consent endpoints (docs/04 §15-§16; docs/02 §11, §13).

Feedback: every target must belong to the session, a duplicate submission
ID is idempotent, a reuse with different content (or another session) is an
idempotency conflict, and provider/configuration context is backend-derived.

Consent: no server-approved notice catalogue or consent store exists in this
build, so consent capture, status, and revocation return the explicit safe
not-ready state; recording stays ``off`` (docs/02 §13 "If consent validation
or storage is unavailable, recording remains off").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from voice_agent.control_api.errors import (
    ApiError,
    ErrorCode,
    dependency_unavailable,
    not_found,
)
from voice_agent.control_api.projections import feedback_receipt
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.schemas.engagement import FeedbackReceipt, FeedbackRequest
from voice_agent.control_api.services.common import load_session
from voice_agent.domain.control_session import SessionRecord, request_fingerprint
from voice_agent.domain.feedback import FeedbackContent, FeedbackRecord
from voice_agent.ports.control_plane import DuplicateKeyError

CONSENT_NOT_AVAILABLE = "Consent capture is not available in this build; recording remains off."


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    receipt: FeedbackReceipt
    created: bool


def _content(request: FeedbackRequest) -> FeedbackContent:
    return FeedbackContent.model_validate(request.model_dump(exclude={"client_submission_id"}))


def _fingerprint(session_id: str, content: FeedbackContent) -> str:
    return request_fingerprint({"session_id": session_id, **content.model_dump(mode="json")})


def _replay(existing: FeedbackRecord, fingerprint: str) -> FeedbackResult:
    if existing.fingerprint != fingerprint:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)
    return FeedbackResult(receipt=feedback_receipt(existing), created=False)


async def _check_targets(
    runtime: ControlPlaneRuntime, session_id: str, content: FeedbackContent
) -> None:
    timeline = runtime.require_stores().timeline
    if content.turn_id is not None:
        turn = await runtime.bounded(timeline.get_turn(session_id, content.turn_id))
        if turn is None:
            raise not_found("The feedback target turn was not found in this session.")
    if content.operation_id is not None:
        view = await runtime.bounded(timeline.get_operation(session_id, content.operation_id))
        if view is None:
            raise not_found("The feedback target operation was not found in this session.")
        if content.turn_id is not None and view.operation.turn_id != content.turn_id:
            raise ApiError(ErrorCode.INVALID_REQUEST, "The operation does not belong to the turn.")


def _record(
    runtime: ControlPlaneRuntime,
    session: SessionRecord,
    request: FeedbackRequest,
    content: FeedbackContent,
    fingerprint: str,
) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=runtime.ids.new_id(),
        client_submission_id=request.client_submission_id,
        fingerprint=fingerprint,
        session_id=session.session_id,
        agent_config_id=session.agent_config_id,
        correlation_id=session.correlation_id,
        environment=session.environment,
        content=content,
        created_at=runtime.clock.utc_now(),
    )


async def submit_feedback(
    runtime: ControlPlaneRuntime, session_id: str, request: FeedbackRequest
) -> FeedbackResult:
    session = await load_session(runtime, session_id)
    feedback = runtime.require_stores().feedback
    content = _content(request)
    fingerprint = _fingerprint(session.session_id, content)
    existing = await runtime.bounded(
        feedback.get_by_client_submission_id(request.client_submission_id)
    )
    if existing is not None:
        return _replay(existing, fingerprint)
    await _check_targets(runtime, session.session_id, content)
    record = _record(runtime, session, request, content, fingerprint)
    duplicate = False
    try:
        await runtime.bounded(feedback.insert(record))
    except DuplicateKeyError:
        duplicate = True
    if duplicate:
        winner = await runtime.bounded(
            feedback.get_by_client_submission_id(request.client_submission_id)
        )
        if winner is None:
            raise ApiError(ErrorCode.REVISION_CONFLICT, retryable=True)
        return _replay(winner, fingerprint)
    return FeedbackResult(receipt=feedback_receipt(record), created=True)


async def consent_unavailable(runtime: ControlPlaneRuntime, session_id: str | None) -> NoReturn:
    runtime.require_stores()
    if session_id is not None:
        await load_session(runtime, session_id)
    raise dependency_unavailable(CONSENT_NOT_AVAILABLE, retryable=False)
