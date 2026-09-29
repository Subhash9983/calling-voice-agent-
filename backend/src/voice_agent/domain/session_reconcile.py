"""Pure reconciliation decision for one nonterminal session (docs/05 §21; docs/04 §9).

The worker is the primary enforcer of timeouts; the control-API reconciler is
the backstop. It never terminalizes a session whose worker lease is still
live: a live worker is only asked to end (maximum duration). Without a live
worker it fails connection timeouts, completes ``ending`` sessions, and fails
an ``active`` session whose worker disappeared (worker-crash recovery is not
part of this build, so the recovery budget is treated as exhausted).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.control_session import SessionRecord

_PRE_ACTIVE: frozenset[SessionStatus] = frozenset({SessionStatus.CREATED, SessionStatus.CONNECTING})


class ReconcileAction(StrEnum):
    NONE = "none"
    REQUEST_MAXIMUM_DURATION_END = "request_maximum_duration_end"
    FAIL_CONNECT_TIMEOUT = "fail_connect_timeout"
    FINALIZE_END = "finalize_end"
    FAIL_WORKER_LOST = "fail_worker_lost"


def reconcile_action(record: SessionRecord, now: datetime) -> ReconcileAction:
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
        return ReconcileAction.FAIL_WORKER_LOST
    return ReconcileAction.NONE
