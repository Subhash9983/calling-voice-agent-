"""Worker assignment lease identity and timings (docs/02 §6; docs/05 §4-§6).

An assignment is identified by ``(generation, worker_instance_id,
livekit_job_id)``; ``writer_epoch`` is the fencing counter and
``lease_revision`` the independent heartbeat compare-and-set counter. A lease
renewal never changes business ``state_revision``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final

from pydantic import Field

from voice_agent.contracts.base import ExternalIdentifier, StrictModel, UtcDatetime

HEARTBEAT_INTERVAL_MS: Final = 5_000
LEASE_DURATION_MS: Final = 15_000
RECONCILE_INTERVAL_MS: Final = 5_000
LOCAL_LEASE_SAFETY_MARGIN_MS: Final = 3_000
INITIAL_GENERATION: Final = 1
INITIAL_WRITER_EPOCH: Final = 1
INITIAL_LEASE_REVISION: Final = 1
MAX_WORKER_RECOVERIES: Final = 1

Counter = Annotated[int, Field(strict=True, ge=1)]


class WorkerClaim(StrictModel):
    """A worker's request to own a session (no secrets or host details)."""

    worker_instance_id: ExternalIdentifier
    livekit_job_id: ExternalIdentifier


class LeaseToken(StrictModel):
    """Everything a heartbeat or fenced write must match."""

    session_id: Annotated[str, Field(min_length=36, max_length=36)]
    generation: Counter
    worker_instance_id: ExternalIdentifier
    livekit_job_id: ExternalIdentifier
    writer_epoch: Counter
    lease_revision: Counter
    lease_expires_at: UtcDatetime

    def renewed(self, *, lease_expires_at: datetime) -> LeaseToken:
        return self.model_copy(
            update={
                "lease_revision": self.lease_revision + 1,
                "lease_expires_at": lease_expires_at,
            }
        )

    def local_deadline(self, sent_at: datetime) -> datetime:
        """Conservative local deadline measured from when the renewal was sent (docs/05 §5)."""
        lease = timedelta(milliseconds=LEASE_DURATION_MS - LOCAL_LEASE_SAFETY_MARGIN_MS)
        return min(self.lease_expires_at, sent_at + lease)


def lease_expiry(now: datetime) -> datetime:
    return now + timedelta(milliseconds=LEASE_DURATION_MS)
