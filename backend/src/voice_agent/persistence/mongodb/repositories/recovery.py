"""Worker-crash recovery compare-and-set operations on ``voice_sessions`` (docs/05 §21; WP10).

Every operation is one atomic, condition-guarded update:

- ``start_recovery``: an ``active`` session with an expired, unreleased lease,
  no termination request, no recovery authorization, and
  ``worker_recovery_count`` below the limit: fence (``writer_epoch`` and
  ``lease_revision`` + 1), count the recovery, set activity ``recovering``,
  and store the authorization (ownership lease 10 s, recovery deadline 20 s,
  owner generation 1, the pre-generated ``recovery_dispatch_id``), and drop
  the dead lease expiry so only the recovery deadlines drive reconciliation;
- ``renew_ownership``: the owner (instance + owner generation) extends its
  unexpired ownership lease by 10 s;
- ``take_over``: after the ownership lease expired and before the recovery
  deadline, fence again and become the owner (owner generation + 1); the
  count, the deadline, and the dispatch ID are kept;
- ``abandon_open_turns`` marks the crashed worker's nonterminal turns
  ``abandoned`` (no buffered speech is ever replayed).

The replacement worker's claim lives with the lease repository.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pymongo import ReturnDocument

from voice_agent.contracts.enums import AgentActivityState, SessionStatus, TurnStatus
from voice_agent.domain.turn import TERMINAL_TURN_STATES
from voice_agent.domain.worker_lease import MAX_WORKER_RECOVERIES
from voice_agent.domain.worker_recovery import (
    RecoveryAuthorization,
    ownership_expiry,
    recovery_deadline,
)
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import from_bson, to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    literal,
    reconcile_stage,
)

_LEASE: Final = "worker_assignment.lease_expires_at"
_EPOCH: Final = "worker_assignment.writer_epoch"
_AUTH: Final = "recovery_authorization"
_OPEN_TURNS: Final = [s.value for s in TurnStatus if s not in TERMINAL_TURN_STATES]
ABANDONMENT_REASON: Final = "worker_lost"


def _plus_one(path: str) -> dict[str, Any]:
    return {"$add": [{"$ifNull": [f"${path}", 0]}, 1]}


def _fence_fields() -> dict[str, Any]:
    return {
        _EPOCH: _plus_one(_EPOCH),
        "worker_assignment.lease_revision": _plus_one("worker_assignment.lease_revision"),
    }


class MongoWorkerRecoveryRepository(MongoRepository):
    async def start_recovery(
        self, session_id: str, *, owner_instance_id: str, recovery_dispatch_id: str, now: datetime
    ) -> RecoveryAuthorization | None:
        filters = {
            "session_id": session_id,
            "status": SessionStatus.ACTIVE.value,
            _LEASE: {"$exists": True},
            "$expr": {"$lte": [f"${_LEASE}", "$$NOW"]},
            "termination_request": {"$exists": False},
            _AUTH: {"$exists": False},
            "worker_recovery_count": {"$lt": MAX_WORKER_RECOVERIES},
        }
        authorization = {
            "owner_instance_id": literal(owner_instance_id),
            "acquired_at": literal(now),
            "expires_at": literal(ownership_expiry(now)),
            "recovery_deadline_at": literal(recovery_deadline(now)),
            "writer_epoch": _plus_one(_EPOCH),
            "recovery_dispatch_id": literal(recovery_dispatch_id),
            "owner_generation": literal(1),
        }
        pipeline: list[dict[str, Any]] = [
            {
                "$set": {
                    _AUTH: authorization,
                    **_fence_fields(),
                    "worker_recovery_count": _plus_one("worker_recovery_count"),
                    "agent_activity_state": literal(AgentActivityState.RECOVERING.value),
                    "state_revision": {"$add": ["$state_revision", 1]},
                    "updated_at": literal(now),
                }
            },
            # The crashed assignment's lease is dead and fenced; dropping its
            # expiry keeps it out of the lease-due scan (docs/02 §19).
            {"$unset": [_LEASE]},
            reconcile_stage(),
        ]
        return await self._authorization(filters, pipeline)

    async def renew_ownership(
        self, session_id: str, *, owner_instance_id: str, owner_generation: int, now: datetime
    ) -> RecoveryAuthorization | None:
        filters = {
            "session_id": session_id,
            f"{_AUTH}.owner_instance_id": owner_instance_id,
            f"{_AUTH}.owner_generation": owner_generation,
            "$expr": {"$gt": [f"${_AUTH}.expires_at", "$$NOW"]},
        }
        pipeline: list[dict[str, Any]] = [
            {"$set": {f"{_AUTH}.expires_at": literal(ownership_expiry(now))}},
            reconcile_stage(),
        ]
        return await self._authorization(filters, pipeline)

    async def take_over(
        self,
        session_id: str,
        *,
        expected_owner_generation: int,
        owner_instance_id: str,
        now: datetime,
    ) -> RecoveryAuthorization | None:
        filters = {
            "session_id": session_id,
            "status": SessionStatus.ACTIVE.value,
            f"{_AUTH}.owner_generation": expected_owner_generation,
            "$and": [
                {"$expr": {"$lte": [f"${_AUTH}.expires_at", "$$NOW"]}},
                {"$expr": {"$gt": [f"${_AUTH}.recovery_deadline_at", "$$NOW"]}},
            ],
        }
        pipeline: list[dict[str, Any]] = [
            {
                "$set": {
                    f"{_AUTH}.owner_instance_id": literal(owner_instance_id),
                    f"{_AUTH}.acquired_at": literal(now),
                    f"{_AUTH}.expires_at": literal(ownership_expiry(now)),
                    f"{_AUTH}.owner_generation": _plus_one(f"{_AUTH}.owner_generation"),
                    f"{_AUTH}.writer_epoch": _plus_one(_EPOCH),
                    **_fence_fields(),
                }
            },
            reconcile_stage(),
        ]
        return await self._authorization(filters, pipeline)

    async def abandon_open_turns(self, session_id: str, *, now: datetime) -> int:
        async with translate_errors():
            result = await self.collection(Collection.CONVERSATION_TURNS).update_many(
                {"session_id": session_id, "status": {"$in": _OPEN_TURNS}},
                {
                    "$set": {
                        "status": TurnStatus.ABANDONED.value,
                        "abandonment_reason": ABANDONMENT_REASON,
                        "updated_at": to_bson(now),
                    },
                    "$inc": {"status_revision": 1},
                },
            )
        return int(result.modified_count)

    async def _authorization(
        self, filters: dict[str, Any], pipeline: list[dict[str, Any]]
    ) -> RecoveryAuthorization | None:
        async with translate_errors():
            stored = await self.collection(Collection.VOICE_SESSIONS).find_one_and_update(
                filters,
                pipeline,
                projection={_AUTH: 1, "_id": 0},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            return None
        return RecoveryAuthorization.model_validate(from_bson(stored)[_AUTH])
