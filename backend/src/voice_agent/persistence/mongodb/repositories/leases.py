"""Worker lease compare-and-set and reconciliation scans (docs/02 §6; docs/05 §4-§6, §21).

The heartbeat path matches the assignment tuple, ``writer_epoch`` and
``lease_revision`` and requires ``lease_expires_at > $$NOW`` (server time).
It increments only ``lease_revision``: ``state_revision``, ``updated_at`` and
``last_activity_at`` are untouched, so a renewal can never cause a business
revision conflict. A clean release removes ``lease_expires_at`` so the
lease-due partial index holds only unreleased assignments.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pymongo import ReturnDocument

from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.worker_lease import (
    INITIAL_GENERATION,
    LeaseToken,
    WorkerClaim,
    lease_expiry,
)
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import from_bson, to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import NONTERMINAL_SESSION_STATES
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    literal,
    reconcile_stage,
)
from voice_agent.ports.session_lifecycle import MAX_RECONCILE_BATCH, ReconcileCandidate

_NONTERMINAL: Final = list(NONTERMINAL_SESSION_STATES)
_ASSIGNMENT: Final = "worker_assignment"
_LEASE: Final = "worker_assignment.lease_expires_at"
_CANDIDATE_PROJECTION: Final = {
    "session_id": 1,
    "status": 1,
    "state_revision": 1,
    "next_reconcile_at": 1,
    "worker_assignment.lease_expires_at": 1,
    "worker_assignment.writer_epoch": 1,
    "_id": 0,
}


def _increment(path: str) -> dict[str, Any]:
    return {"$add": [{"$ifNull": [f"${path}", 0]}, 1]}


def _token_filter(token: LeaseToken) -> dict[str, Any]:
    return {
        "session_id": token.session_id,
        "worker_assignment.generation": token.generation,
        "worker_assignment.worker_instance_id": token.worker_instance_id,
        "worker_assignment.livekit_job_id": token.livekit_job_id,
        "worker_assignment.writer_epoch": token.writer_epoch,
    }


class MongoWorkerLeaseRepository(MongoRepository):
    async def claim_initial(
        self, session_id: str, claim: WorkerClaim, *, expected_revision: int, now: datetime
    ) -> LeaseToken | None:
        expires = lease_expiry(now)
        filters = {
            "session_id": session_id,
            "state_revision": expected_revision,
            "status": SessionStatus.CONNECTING.value,
            "termination_request": {"$exists": False},
            "recovery_authorization": {"$exists": False},
            "$or": [
                {_LEASE: {"$exists": False}},
                {"$expr": {"$lte": [f"${_LEASE}", "$$NOW"]}},
            ],
        }
        assignment = {
            "worker_instance_id": literal(claim.worker_instance_id),
            "livekit_job_id": literal(claim.livekit_job_id),
            "generation": literal(INITIAL_GENERATION),
            "writer_epoch": _increment("worker_assignment.writer_epoch"),
            "lease_revision": _increment("worker_assignment.lease_revision"),
            "claimed_at": literal(now),
            "heartbeat_at": literal(now),
            "lease_expires_at": literal(expires),
        }
        pipeline: list[dict[str, Any]] = [
            {"$set": {"_claim": assignment}},
            {"$unset": [_ASSIGNMENT]},
            {
                "$set": {
                    _ASSIGNMENT: "$_claim",
                    "state_revision": {"$add": ["$state_revision", 1]},
                    "updated_at": literal(now),
                }
            },
            {"$unset": ["_claim"]},
            reconcile_stage(),
        ]
        stored = await self._update_returning(filters, pipeline)
        return None if stored is None else _token(session_id, stored)

    async def claim_recovery(
        self, session_id: str, claim: WorkerClaim, *, recovery_dispatch_id: str, now: datetime
    ) -> LeaseToken | None:
        """Replacement claim under the stored recovery authorization (docs/05 §21 step 7).

        Requires ``active``, the matching ``recovery_dispatch_id``, an unexpired
        ``recovery_deadline_at``, and no termination request (never the
        reconciler's ownership lease). Writes generation + 1 under a higher
        writer epoch, resumes ``listening``, and unsets the authorization.
        """
        filters = {
            "session_id": session_id,
            "status": SessionStatus.ACTIVE.value,
            "termination_request": {"$exists": False},
            "recovery_authorization.recovery_dispatch_id": recovery_dispatch_id,
            "$expr": {"$gt": ["$recovery_authorization.recovery_deadline_at", "$$NOW"]},
        }
        assignment = {
            "worker_instance_id": literal(claim.worker_instance_id),
            "livekit_job_id": literal(claim.livekit_job_id),
            "generation": _increment("worker_assignment.generation"),
            "writer_epoch": _increment("worker_assignment.writer_epoch"),
            "lease_revision": _increment("worker_assignment.lease_revision"),
            "claimed_at": literal(now),
            "heartbeat_at": literal(now),
            "lease_expires_at": literal(lease_expiry(now)),
        }
        pipeline: list[dict[str, Any]] = [
            {"$set": {"_claim": assignment}},
            {"$unset": [_ASSIGNMENT, "recovery_authorization"]},
            {
                "$set": {
                    _ASSIGNMENT: "$_claim",
                    "agent_activity_state": literal("listening"),
                    "state_revision": {"$add": ["$state_revision", 1]},
                    "updated_at": literal(now),
                }
            },
            {"$unset": ["_claim"]},
            reconcile_stage(),
        ]
        stored = await self._update_returning(filters, pipeline)
        return None if stored is None else _token(session_id, stored)

    async def renew_lease(self, token: LeaseToken, *, now: datetime) -> LeaseToken | None:
        expires = lease_expiry(now)
        filters = {
            **_token_filter(token),
            "worker_assignment.lease_revision": token.lease_revision,
            "status": {"$in": _NONTERMINAL},
            "$expr": {"$gt": [f"${_LEASE}", "$$NOW"]},
        }
        pipeline: list[dict[str, Any]] = [
            {
                "$set": {
                    "worker_assignment.heartbeat_at": literal(now),
                    _LEASE: literal(expires),
                    "worker_assignment.lease_revision": _increment(
                        "worker_assignment.lease_revision"
                    ),
                }
            },
            reconcile_stage(),
        ]
        stored = await self._update_returning(filters, pipeline)
        return None if stored is None else _token(token.session_id, stored)

    async def release(self, token: LeaseToken, *, now: datetime) -> bool:
        filters = {**_token_filter(token), _LEASE: {"$exists": True}}
        pipeline: list[dict[str, Any]] = [
            {
                "$set": {
                    "worker_assignment.released_at": literal(now),
                    "worker_assignment.lease_revision": _increment(
                        "worker_assignment.lease_revision"
                    ),
                }
            },
            {"$unset": [_LEASE]},
            reconcile_stage(),
        ]
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).update_one(filters, pipeline)
        return result.matched_count == 1

    async def fence_expired(
        self, session_id: str, *, expected_writer_epoch: int, now: datetime
    ) -> int | None:
        filters = {
            "session_id": session_id,
            "worker_assignment.writer_epoch": expected_writer_epoch,
            _LEASE: {"$exists": True},
            "$expr": {"$lte": [f"${_LEASE}", "$$NOW"]},
        }
        pipeline: list[dict[str, Any]] = [
            {
                "$set": {
                    "worker_assignment.writer_epoch": _increment("worker_assignment.writer_epoch"),
                    "worker_assignment.lease_revision": _increment(
                        "worker_assignment.lease_revision"
                    ),
                }
            }
        ]
        stored = await self._update_returning(filters, pipeline)
        if stored is None:
            return None
        return int(stored["worker_assignment"]["writer_epoch"])

    async def _update_returning(
        self, filters: dict[str, Any], pipeline: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        async with translate_errors():
            stored = await self.collection(Collection.VOICE_SESSIONS).find_one_and_update(
                filters,
                pipeline,
                projection={_ASSIGNMENT: 1, "_id": 0},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            return None
        converted: dict[str, Any] = from_bson(stored)
        return converted


def _token(session_id: str, stored: dict[str, Any]) -> LeaseToken:
    assignment = stored[_ASSIGNMENT]
    return LeaseToken(
        session_id=session_id,
        generation=assignment["generation"],
        worker_instance_id=assignment["worker_instance_id"],
        livekit_job_id=assignment["livekit_job_id"],
        writer_epoch=assignment["writer_epoch"],
        lease_revision=assignment["lease_revision"],
        lease_expires_at=assignment["lease_expires_at"],
    )


class MongoSessionReconciliationRepository(MongoRepository):
    async def list_reconcile_due(
        self, environment: str, *, now: datetime, limit: int
    ) -> Sequence[ReconcileCandidate]:
        filters = {
            "environment": environment,
            "status": {"$in": _NONTERMINAL},
            "next_reconcile_at": {"$lte": to_bson(now)},
        }
        return await self._scan(filters, "next_reconcile_at", "ix_sessions_reconcile_due", limit)

    async def list_lease_due(
        self, environment: str, *, now: datetime, limit: int
    ) -> Sequence[ReconcileCandidate]:
        filters = {
            "environment": environment,
            "status": {"$in": _NONTERMINAL},
            _LEASE: {"$lte": to_bson(now)},
        }
        return await self._scan(filters, _LEASE, "ix_sessions_lease_due", limit)

    async def _scan(
        self, filters: dict[str, Any], sort_field: str, index: str, limit: int
    ) -> list[ReconcileCandidate]:
        batch = bounded_limit(limit, MAX_RECONCILE_BATCH)
        async with translate_errors():
            rows = await (
                self.collection(Collection.VOICE_SESSIONS)
                .find(
                    filters,
                    projection=_CANDIDATE_PROJECTION,
                    sort=[(sort_field, 1)],
                    limit=batch,
                    hint=index,
                )
                .to_list(length=batch)
            )
        return [_candidate(from_bson(row)) for row in rows]


def _candidate(row: dict[str, Any]) -> ReconcileCandidate:
    assignment = row.get(_ASSIGNMENT) or {}
    return ReconcileCandidate(
        session_id=row["session_id"],
        status=SessionStatus(row["status"]),
        state_revision=row["state_revision"],
        next_reconcile_at=row.get("next_reconcile_at"),
        lease_expires_at=assignment.get("lease_expires_at"),
        writer_epoch=assignment.get("writer_epoch"),
    )
