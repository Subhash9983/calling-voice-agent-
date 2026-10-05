"""Approved session timeouts for the conversation orchestrator (docs/02 §24, docs/01 §16).

All values come from the immutable configuration's ``timeout_policy``:

- ``maximum_silence_ms`` (60 s): no speech or agent output while listening
  -> agent activity ``idle`` (the session stays open);
- ``maximum_user_turn_ms`` (120 s): an open user turn is committed;
- ``idle_session_ms`` (300 s): no speech or agent output -> the session ends
  ``idle_timeout`` (``ended``);
- the maximum session duration (30 min): the time-limit notice is spoken
  ``time_limit_notice_ms`` before the durable deadline, then the session ends
  ``maximum_duration`` (the runner and the reconciler stay the backstops);
- the 20 s reconnect window is owned by the transport (WP6) and is not
  repeated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from voice_agent.domain.agent_config import TimeoutPolicySection

# Long enough to synthesize and play the ~3 s time-limit phrase before the
# runner's own deadline stop and the reconciler's maximum-duration end request.
DEFAULT_TIME_LIMIT_NOTICE_MS: Final = 8_000
DEFAULT_TICK_S: Final = 1.0


@dataclass(frozen=True, slots=True)
class ConversationTimeouts:
    maximum_silence_ms: int
    maximum_user_turn_ms: int
    idle_session_ms: int
    maximum_duration_deadline_at: datetime | None
    time_limit_notice_ms: int = DEFAULT_TIME_LIMIT_NOTICE_MS
    tick_s: float = DEFAULT_TICK_S

    @classmethod
    def from_policy(
        cls, policy: TimeoutPolicySection, *, deadline_at: datetime | None
    ) -> ConversationTimeouts:
        return cls(
            maximum_silence_ms=policy.maximum_silence_ms,
            maximum_user_turn_ms=policy.maximum_user_turn_ms,
            idle_session_ms=policy.idle_session_ms,
            maximum_duration_deadline_at=deadline_at,
        )
