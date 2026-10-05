"""Pure reconciliation decision for one nonterminal session (docs/05 §21; docs/04 §9).

The worker is the primary enforcer of timeouts; the control-API reconciler is
the backstop. It never terminalizes a session whose worker lease is still
live: a live worker is only asked to end (maximum duration). Without a live
worker it fails connection timeouts, completes ``ending`` sessions, and for an
``active`` session whose worker disappeared applies the bounded worker-crash
recovery contract (WP10, Decision 067):

- at most one recovery per session: an expired lease with no recovery
  authorization and ``worker_recovery_count`` below the limit starts one,
  when this reconciler can recover (otherwise the session fails);
- the owning reconciler continues it (renew ownership, ensure the single
  replacement dispatch); another instance takes over only after the 10 s
  ownership lease expired;
- once ``recovery_deadline_at`` (20 s after first acquisition) passes without
  a successful replacement claim, any instance fails the session;
- a second worker crash (the count is exhausted) fails the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.worker_lease import MAX_WORKER_RECOVERIES

_PRE_ACTIVE: frozenset[SessionStatus] = frozenset({SessionStatus.CREATED, SessionStatus.CONNECTING})


class ReconcileAction(StrEnum):
    NONE = "none"
    REQUEST_MAXIMUM_DURATION_END = "request_maximum_duration_end"
    FAIL_CONNECT_TIMEOUT = "fail_connect_timeout"
    FINALIZE_END = "finalize_end"
    FAIL_WORKER_LOST = "fail_worker_lost"
    START_RECOVERY = "start_recovery"
    CONTINUE_RECOVERY = "continue_recovery"
    TAKE_OVER_RECOVERY = "take_over_recovery"
    FAIL_RECOVERY_EXHAUSTED = "fail_recovery_exhausted"


@dataclass(frozen=True, slots=True)
class RecoveryCapability:
    """This reconciler can run worker-crash recovery as ``instance_id``."""

    instance_id: str


def _active_action(
    record: SessionRecord, now: datetime, recovery: RecoveryCapability | None
) -> ReconcileAction:
    authorization = record.recovery_authorization
    if authorization is not None:
        if authorization.deadline_passed(now):
            return ReconcileAction.FAIL_RECOVERY_EXHAUSTED
        if recovery is None:
            return ReconcileAction.NONE
        if authorization.ownership_expired(now):
            return ReconcileAction.TAKE_OVER_RECOVERY
        if authorization.owned_by(recovery.instance_id, now):
            return ReconcileAction.CONTINUE_RECOVERY
        return ReconcileAction.NONE
    can_recover = (
        recovery is not None
        and record.termination_request is None
        and record.worker_recovery_count < MAX_WORKER_RECOVERIES
        and record.worker_lease_expires_at is not None
    )
    return ReconcileAction.START_RECOVERY if can_recover else ReconcileAction.FAIL_WORKER_LOST


def reconcile_action(
    record: SessionRecord, now: datetime, *, recovery: RecoveryCapability | None = None
) -> ReconcileAction:
    if record.is_terminal:
        return ReconcileAction.NONE
    live = record.has_live_worker(now)
    if record.status is SessionStatus.ENDING:
        return ReconcileAction.NONE if live else ReconcileAction.FINALIZE_END
    if record.maximum_duration_reached(now):
        return ReconcileAction.REQUEST_MAXIMUM_DURATION_END
    if live:
        return ReconcileAction.NONE
    deadline = record.connect_deadline_at
    if record.status in _PRE_ACTIVE and deadline is not None and now >= deadline:
        return ReconcileAction.FAIL_CONNECT_TIMEOUT
    if record.status is SessionStatus.ACTIVE:
        return _active_action(record, now, recovery)
    return ReconcileAction.NONE
