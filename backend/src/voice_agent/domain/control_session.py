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

MAX_JOIN_TOKEN_REQUESTS = 10
FINGERPRINT_PREFIX = "sha256:"
# A join credential is issued only while the session is nonterminal and its
# room exists (docs/04 §6-§7: no token for terminal sessions).
JOINABLE_STATES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.CONNECTING, SessionStatus.ACTIVE, SessionStatus.ENDING}
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

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SESSION_STATES

    def can_issue_join_token(self, now: datetime) -> bool:
        return (
            self.status in JOINABLE_STATES
            and self.transport is not None
            and not self.maximum_duration_reached(now)
        )

    def maximum_duration_reached(self, now: datetime) -> bool:
        return now - self.created_at >= timedelta(milliseconds=self.maximum_session_ms)

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
        return self._transition(SessionStatus.FAILED, now, disconnect_reason=reason, ended_at=now)

    def request_end(
        self,
        *,
        client_request_id: str,
        reason: DisconnectReason,
        requested_by: TerminationRequester,
        now: datetime,
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
            SessionStatus.ENDING, now, termination_request=request, ending_at=now
        )
        return EndDecision(record=ending, accepted=True)

    def record_join_token_request(
        self, *, client_request_id: str, fingerprint: str, now: datetime
    ) -> JoinDecision:
        """Record bounded join-token evidence; never stores the token (docs/02 §6)."""
        if not self.can_issue_join_token(now):
            raise LifecycleStateError("session is not joinable")
        entries = list(self.join_token_requests)
        match = next((e for e in entries if e.client_request_id == client_request_id), None)
        if match is not None and match.fingerprint != fingerprint:
            raise IdempotencyConflictError("join-token request reused with other semantics")
        if match is None:
            entry = JoinTokenRequestEntry(
                client_request_id=client_request_id,
                fingerprint=fingerprint,
                first_requested_at=now,
                last_issued_at=now,
                issue_count=1,
            )
        else:
            entries.remove(match)
            entry = match.model_copy(
                update={"last_issued_at": now, "issue_count": match.issue_count + 1}
            )
        kept = (*entries, entry)[-MAX_JOIN_TOKEN_REQUESTS:]
        updated = self.model_copy(update={"join_token_requests": kept, "updated_at": now})
        return JoinDecision(record=updated, replay=match is not None)
