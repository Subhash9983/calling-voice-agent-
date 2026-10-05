"""Worker-crash recovery authorization (docs/05 §21, Decision 067; docs/02 §6).

After a worker lease expires, the control-API reconciler fences the old
writer epoch and stores ``recovery_authorization``; it then creates at most
one replacement explicit dispatch carrying ``recovery_dispatch_id``. The
replacement claim needs the matching dispatch ID and an unexpired
``recovery_deadline_at`` (never the reconciler ownership lease). A session
gets at most one worker-crash recovery (``worker_recovery_count`` <= 1).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final

from pydantic import Field

from voice_agent.contracts.base import ExternalIdentifier, StrictModel, UtcDatetime

RECOVERY_OWNERSHIP_MS: Final = 10_000
RECOVERY_WINDOW_MS: Final = 20_000

Counter = Annotated[int, Field(strict=True, ge=1)]


class RecoveryAuthorization(StrictModel):
    owner_instance_id: ExternalIdentifier
    acquired_at: UtcDatetime
    expires_at: UtcDatetime
    recovery_deadline_at: UtcDatetime
    writer_epoch: Counter
    recovery_dispatch_id: ExternalIdentifier
    owner_generation: Counter

    def owned_by(self, instance_id: str, now: datetime) -> bool:
        """``instance_id`` holds the unexpired ownership lease."""
        return self.owner_instance_id == instance_id and self.expires_at > now

    def deadline_passed(self, now: datetime) -> bool:
        return now >= self.recovery_deadline_at

    def ownership_expired(self, now: datetime) -> bool:
        return self.expires_at <= now


def ownership_expiry(now: datetime) -> datetime:
    return now + timedelta(milliseconds=RECOVERY_OWNERSHIP_MS)


def recovery_deadline(now: datetime) -> datetime:
    return now + timedelta(milliseconds=RECOVERY_WINDOW_MS)
