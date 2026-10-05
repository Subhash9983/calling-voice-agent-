"""Control-plane voice-session record and its idempotency rules (docs/02 §6; docs/04 §6-§9).

``SessionRecord`` is the durable quick-summary shape the control API owns: the
create idempotency key and fingerprint, the backend-owned transport identity,
the bounded join-token request evidence, and the authoritative termination
request. Every method returns a new immutable record. The record never holds
a join token, credential, or dispatch metadata blob.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    ShortLabel,
    StrictModel,
    UtcDatetime,
)
from voice_agent.contracts.enums import AgentActivityState, DisconnectReason, SessionStatus
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
    LifecycleStateError,
)
from voice_agent.domain.session import SESSION_TRANSITIONS, TERMINAL_SESSION_STATES
from voice_agent.domain.worker_recovery import RecoveryAuthorization

MAX_JOIN_TOKEN_REQUESTS = 10
FINGERPRINT_PREFIX = "sha256:"
# Approved default graceful-shutdown bound (docs/02 §24) used as the
# termination deadline after an end request is accepted.
DEFAULT_TERMINATION_GRACE_MS = 10_000
# A join credential is issued only while the session is nonterminal and its
# room exists (docs/04 §6-§7: no token for terminal sessions).
JOINABLE_STATES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.CONNECTING, SessionStatus.ACTIVE, SessionStatus.ENDING}
)

# End reasons that complete as ``ended``; agent-side or infrastructure faults
# complete as ``failed`` (docs/01 §5, docs/04 §9 bounded completion rules).
USER_SIDE_END_REASONS: frozenset[DisconnectReason] = frozenset(
    {
        DisconnectReason.USER_ENDED,
        DisconnectReason.BROWSER_CLOSED,
        DisconnectReason.NETWORK_LOST,
        DisconnectReason.IDLE_TIMEOUT,
        DisconnectReason.MAXIMUM_DURATION,
    }
)

Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=0)]


class InitiatorType(StrEnum):
    INTERNAL_TESTER = "internal_tester"


class TerminationRequester(StrEnum):
    ANONYMOUS_USER = "anonymous_user"
    SYSTEM_TIMEOUT = "system_timeout"
    SYSTEM_RECONCILER = "system_reconciler"
    SYSTEM_EVALUATION = "system_evaluation"


def request_fingerprint(semantics: Mapping[str, JsonValue]) -> str:
    """Canonical SHA-256 fingerprint of a request's semantic fields."""
    canonical = json.dumps(semantics, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return FINGERPRINT_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_request_fingerprint(
    *, agent_config_id: str, channel: str, session_mode: str, language_mode: str
) -> str:
    """Fingerprint of the session-create semantics (everything but the request ID).

    Every input is a stored session field, so persistence re-derives the
    fingerprint instead of storing an unapproved root field (docs/02 §6).
    """
    return request_fingerprint(
        {
            "agent_config_id": agent_config_id,
            "channel": channel,
            "session_mode": session_mode,
            "language_mode": language_mode,
        }
    )


class JoinTokenOutcome(StrEnum):
    """Result of recording one join-token request (docs/02 §6)."""

    RECORDED = "recorded"
    REPLAYED = "replayed"
    FINGERPRINT_CONFLICT = "fingerprint_conflict"


class ComponentSnapshot(StrictModel):
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    adapter_version: ExternalIdentifier


class ProviderSnapshot(StrictModel):
    transport: ComponentSnapshot
    stt: ComponentSnapshot
    conversation_engine: ComponentSnapshot
    tts: ComponentSnapshot


class TransportBinding(StrictModel):
    """Opaque backend-owned room/dispatch/participant identity (never a token)."""

    provider: ShortLabel
    external_room_id: ExternalIdentifier
    external_session_id: ExternalIdentifier | None = None
    browser_participant_id: ExternalIdentifier
    # Opaque expected agent identity (docs/06 §7, §21); optional so bindings
    # created before WP6 (mock transport) remain valid.
    agent_participant_id: ExternalIdentifier | None = None


class TerminationRequest(StrictModel):
    client_request_id: CanonicalId
    reason: DisconnectReason
    requested_by: TerminationRequester
    requested_at: UtcDatetime
    revision: Annotated[int, Field(strict=True, ge=1)]


class JoinTokenRequestEntry(StrictModel):
    client_request_id: CanonicalId
    fingerprint: Fingerprint
    first_requested_at: UtcDatetime
    last_issued_at: UtcDatetime
    issue_count: Annotated[int, Field(strict=True, ge=1)]


def apply_join_token_request(
    entries: tuple[JoinTokenRequestEntry, ...],
    *,
    client_request_id: str,
    fingerprint: str,
    now: datetime,
) -> tuple[tuple[JoinTokenRequestEntry, ...], JoinTokenOutcome]:
    """Record one request in the bounded list, keeping the 10 most recent issues.

    Stores apply exactly this rule atomically (MongoDB: positional update or
    ``$push`` with ``$sort``/``$slice``) so concurrent refreshes keep every entry.
    """
    match = next((e for e in entries if e.client_request_id == client_request_id), None)
    if match is not None and match.fingerprint != fingerprint:
        return entries, JoinTokenOutcome.FINGERPRINT_CONFLICT
    if match is None:
        entry = JoinTokenRequestEntry(
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            first_requested_at=now,
            last_issued_at=now,
            issue_count=1,
        )
        outcome = JoinTokenOutcome.RECORDED
    else:
        entry = match.model_copy(
            update={"last_issued_at": now, "issue_count": match.issue_count + 1}
        )
        outcome = JoinTokenOutcome.REPLAYED
    others = [e for e in entries if e.client_request_id != client_request_id]
    ordered = sorted([*others, entry], key=lambda e: e.last_issued_at)
    return tuple(ordered[-MAX_JOIN_TOKEN_REQUESTS:]), outcome


@dataclass(frozen=True, slots=True)
class EndDecision:
    record: SessionRecord
    accepted: bool


@dataclass(frozen=True, slots=True)
class JoinDecision:
    record: SessionRecord
    replay: bool


class SessionRecord(StrictModel):
    session_id: CanonicalId
    client_request_id: CanonicalId
    create_fingerprint: Fingerprint
    correlation_id: ExternalIdentifier
    agent_id: CanonicalId
    agent_config_id: CanonicalId
    agent_config_version: Annotated[int, Field(strict=True, ge=1)]
    config_checksum: Fingerprint
    environment: AgentConfigEnvironment
    channel: Literal["browser"] = "browser"
    session_mode: Literal["interactive_test"] = "interactive_test"
    language_mode: ShortLabel
    initiator_type: InitiatorType = InitiatorType.INTERNAL_TESTER
    status: SessionStatus = SessionStatus.CREATED
    agent_activity_state: AgentActivityState | None = None
    state_revision: Revision = 0
    disconnect_reason: DisconnectReason | None = None
    termination_request: TerminationRequest | None = None
    join_token_requests: Annotated[
        tuple[JoinTokenRequestEntry, ...], Field(max_length=MAX_JOIN_TOKEN_REQUESTS)
    ] = ()
    transport: TransportBinding | None = None
    provider_snapshot: ProviderSnapshot
    cost_currency: ShortLabel
    cost_rate_card_version: ExternalIdentifier
    recording_mode: Literal["off"] = "off"
    recording_status: Literal["not_requested"] = "not_requested"
    maximum_session_ms: Annotated[int, Field(strict=True, gt=0)]
    created_at: UtcDatetime
    updated_at: UtcDatetime
    connecting_at: UtcDatetime | None = None
    ending_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    # Reconciler deadlines (docs/02 §6); each is present only while it is an
    # active deadline for the current state.
    connect_deadline_at: UtcDatetime | None = None
    termination_deadline_at: UtcDatetime | None = None
    # Store-owned and read-only here: the unreleased worker lease expiry
    # (docs/02 §6 ``worker_assignment.lease_expires_at``). A record replace
    # never writes it; only the lease repository does.
    worker_lease_expires_at: UtcDatetime | None = None
    # Store-owned and read-only here as well (docs/05 §21): the stored
    # worker-crash recovery authorization and the recovery count.
    recovery_authorization: RecoveryAuthorization | None = None
    worker_recovery_count: Annotated[int, Field(strict=True, ge=0)] = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SESSION_STATES

    @property
    def maximum_duration_deadline_at(self) -> datetime:
        return self.created_at + timedelta(milliseconds=self.maximum_session_ms)

    @property
    def next_reconcile_at(self) -> datetime | None:
        """Earliest business deadline; storage also merges worker/recovery leases."""
        if self.is_terminal:
            return None
        candidates = (
            self.connect_deadline_at,
            self.termination_deadline_at,
            self.maximum_duration_deadline_at,
        )
        return min(value for value in candidates if value is not None)

    def can_issue_join_token(self, now: datetime) -> bool:
        return (
            self.status in JOINABLE_STATES
            and self.transport is not None
            and not self.maximum_duration_reached(now)
        )

    def maximum_duration_reached(self, now: datetime) -> bool:
        return now - self.created_at >= timedelta(milliseconds=self.maximum_session_ms)

    def has_live_worker(self, now: datetime) -> bool:
        """A worker lease exists and has not expired (docs/04 §9 fast-signal rule)."""
        expires = self.worker_lease_expires_at
        return expires is not None and expires > now

    def _transition(self, target: SessionStatus, now: datetime, **update: object) -> SessionRecord:
        if target not in SESSION_TRANSITIONS[self.status]:
            raise InvalidTransitionError("session", self.status.value, target.value)
        return self.model_copy(
            update={
                "status": target,
                "state_revision": self.state_revision + 1,
                "updated_at": now,
                **update,
            }
        )

    def bind_transport(self, binding: TransportBinding, *, now: datetime) -> SessionRecord:
        """Record the prepared room/dispatch and move ``created -> connecting``."""
        return self._transition(SessionStatus.CONNECTING, now, transport=binding, connecting_at=now)

    def fail(self, reason: DisconnectReason, *, now: datetime) -> SessionRecord:
        return self._transition(
            SessionStatus.FAILED,
            now,
            disconnect_reason=reason,
            ended_at=now,
            connect_deadline_at=None,
            termination_deadline_at=None,
        )

    def finalize_end(self, *, now: datetime) -> SessionRecord:
        """Complete an ``ending`` session as ``ended`` or controlled ``failed`` (docs/01 §5)."""
        request = self.termination_request
        if self.status is not SessionStatus.ENDING or request is None:
            raise LifecycleStateError("only an ending session with a request can be finalized")
        target = (
            SessionStatus.ENDED if request.reason in USER_SIDE_END_REASONS else SessionStatus.FAILED
        )
        return self._transition(
            target,
            now,
            disconnect_reason=request.reason,
            ended_at=now,
            connect_deadline_at=None,
            termination_deadline_at=None,
        )

    def request_end(
        self,
        *,
        client_request_id: str,
        reason: DisconnectReason,
        requested_by: TerminationRequester,
        now: datetime,
        termination_grace_ms: int = DEFAULT_TERMINATION_GRACE_MS,
    ) -> EndDecision:
        """Create or reuse the authoritative termination request (docs/04 §9)."""
        existing = self.termination_request
        if existing is not None:
            if existing.client_request_id == client_request_id and existing.reason != reason:
                raise IdempotencyConflictError("termination request reused with another reason")
            return EndDecision(record=self, accepted=False)
        if self.is_terminal:
            return EndDecision(record=self, accepted=False)
        request = TerminationRequest(
            client_request_id=client_request_id,
            reason=reason,
            requested_by=requested_by,
            requested_at=now,
            revision=1,
        )
        ending = self._transition(
            SessionStatus.ENDING,
            now,
            termination_request=request,
            ending_at=now,
            connect_deadline_at=None,
            termination_deadline_at=now + timedelta(milliseconds=termination_grace_ms),
        )
        return EndDecision(record=ending, accepted=True)

    def record_join_token_request(
        self, *, client_request_id: str, fingerprint: str, now: datetime
    ) -> JoinDecision:
        """Validate and preview one join-token request; never stores the token (docs/02 §6).

        This is the snapshot decision. Stores persist the entry atomically
        with :func:`apply_join_token_request`, whose outcome is authoritative.
        """
        if not self.can_issue_join_token(now):
            raise LifecycleStateError("session is not joinable")
        entries, outcome = apply_join_token_request(
            self.join_token_requests,
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            now=now,
        )
        if outcome is JoinTokenOutcome.FINGERPRINT_CONFLICT:
            raise IdempotencyConflictError("join-token request reused with other semantics")
        updated = self.model_copy(update={"join_token_requests": entries, "updated_at": now})
        return JoinDecision(record=updated, replay=outcome is JoinTokenOutcome.REPLAYED)
