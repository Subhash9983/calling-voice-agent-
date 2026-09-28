"""Voice session state machine (docs/01 §5-§6, docs/02 §6).

Transitions return a new immutable session; terminal states never return
to an active state.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import Field

from voice_agent.contracts.base import CanonicalId, StrictModel
from voice_agent.contracts.enums import AgentActivityState, DisconnectReason, SessionStatus
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError

SESSION_TRANSITIONS: Mapping[SessionStatus, frozenset[SessionStatus]] = MappingProxyType(
    {
        SessionStatus.CREATED: frozenset(
            {SessionStatus.CONNECTING, SessionStatus.ENDING, SessionStatus.FAILED}
        ),
        SessionStatus.CONNECTING: frozenset(
            {SessionStatus.ACTIVE, SessionStatus.ENDING, SessionStatus.FAILED}
        ),
        SessionStatus.ACTIVE: frozenset({SessionStatus.ENDING, SessionStatus.FAILED}),
        SessionStatus.ENDING: frozenset({SessionStatus.ENDED, SessionStatus.FAILED}),
        SessionStatus.ENDED: frozenset(),
        SessionStatus.FAILED: frozenset(),
    }
)
TERMINAL_SESSION_STATES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.ENDED, SessionStatus.FAILED}
)


class VoiceSession(StrictModel):
    session_id: CanonicalId
    correlation_id: Annotated[str, Field(min_length=1, max_length=256)]
    status: SessionStatus = SessionStatus.CREATED
    agent_activity_state: AgentActivityState | None = None
    state_revision: Annotated[int, Field(ge=0)] = 0
    disconnect_reason: DisconnectReason | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SESSION_STATES

    def can_transition_to(self, target: SessionStatus) -> bool:
        return target in SESSION_TRANSITIONS[self.status]

    def transition_to(
        self, target: SessionStatus, *, disconnect_reason: DisconnectReason | None = None
    ) -> VoiceSession:
        if not self.can_transition_to(target):
            raise InvalidTransitionError("session", self.status.value, target.value)
        update: dict[str, object] = {
            "status": target,
            "state_revision": self.state_revision + 1,
        }
        if disconnect_reason is not None:
            update["disconnect_reason"] = disconnect_reason
        if target is SessionStatus.ACTIVE:
            update["agent_activity_state"] = AgentActivityState.LISTENING
        return self.model_copy(update=update)

    def with_activity(self, activity: AgentActivityState) -> VoiceSession:
        """Agent activity is meaningful only while the session is active (docs/01 §6)."""
        if self.status is not SessionStatus.ACTIVE:
            raise DomainRuleError("agent activity can change only while the session is active")
        if activity is self.agent_activity_state:
            return self
        return self.model_copy(
            update={"agent_activity_state": activity, "state_revision": self.state_revision + 1}
        )
