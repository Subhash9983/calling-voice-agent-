"""Worker-side ``SessionRepository`` over ``voice_sessions`` (docs/02 §6; docs/05 §4).

The worker only ever updates a session the control API created. Every
write is fenced by the claimed ``writer_epoch`` and assignment generation
and rejects a stale ``state_revision``; a terminal transition sets the
retention anchor and propagates it to children.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.worker_lease import LeaseToken
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import from_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    literal_set_stage,
    reconcile_stage,
    unset_stage,
)
from voice_agent.persistence.mongodb.repositories.retention import SessionExpiryPropagator
from voice_agent.ports.clock import Clock
from voice_agent.ports.persistence import ReferenceNotFoundError, WriterFencedError
from voice_agent.ports.repositories import RevisionConflictError
from voice_agent.privacy_and_retention.expiry import session_expires_at


class MongoWorkerSessionRepository(MongoRepository):
    def __init__(
        self, persistence: MongoPersistence, *, clock: Clock, fence: LeaseToken | None
    ) -> None:
        super().__init__(persistence)
        self._clock = clock
        self._fence = fence
        self._expiry = SessionExpiryPropagator(persistence)

    async def get(self, session_id: str) -> VoiceSession | None:
        async with translate_errors():
            raw = await self.collection(Collection.VOICE_SESSIONS).find_one(
                {"session_id": session_id},
                projection={
                    "session_id": 1,
                    "correlation_id": 1,
                    "status": 1,
                    "agent_activity_state": 1,
                    "state_revision": 1,
                    "disconnect_reason": 1,
                    "_id": 0,
                },
            )
        return None if raw is None else VoiceSession.model_validate(from_bson(raw))

    async def save(self, session: VoiceSession) -> None:
        now = self._clock.utc_now()
        filters: dict[str, Any] = {
            "session_id": session.session_id,
            # A terminal transition is written once; other saves may repeat.
            "state_revision": {"$lt" if session.is_terminal else "$lte": session.state_revision},
            **self._fence_filter(),
        }
        to_set, to_unset = self._fields(session, now)
        pipeline = [literal_set_stage(to_set), *unset_stage(to_unset), reconcile_stage()]
        async with translate_errors():
            result = await self.collection(Collection.VOICE_SESSIONS).update_one(filters, pipeline)
        if result.matched_count == 0:
            await self._diagnose(session)
        if session.is_terminal:
            await self._expiry.propagate_session_expiry(session.session_id)

    def _fence_filter(self) -> dict[str, Any]:
        if self._fence is None:
            return {}
        return {
            "worker_assignment.writer_epoch": self._fence.writer_epoch,
            "worker_assignment.generation": self._fence.generation,
        }

    @staticmethod
    def _fields(session: VoiceSession, now: datetime) -> tuple[dict[str, Any], list[str]]:
        to_set: dict[str, Any] = {
            "status": session.status,
            "state_revision": session.state_revision,
            "updated_at": now,
        }
        to_unset: list[str] = []
        for name in ("agent_activity_state", "disconnect_reason"):
            value = getattr(session, name)
            if value is None:
                to_unset.append(name)
            else:
                to_set[name] = value
        if session.status is SessionStatus.ACTIVE:
            to_unset.append("connect_deadline_at")
        if session.is_terminal:
            to_set.update({"ended_at": now, "expires_at": session_expires_at(now)})
            to_unset.extend(["connect_deadline_at", "termination_deadline_at", "idle_deadline_at"])
        return to_set, to_unset

    async def _diagnose(self, session: VoiceSession) -> None:
        async with translate_errors():
            raw = await self.collection(Collection.VOICE_SESSIONS).find_one(
                {"session_id": session.session_id},
                projection={"state_revision": 1, "status": 1, "worker_assignment": 1, "_id": 0},
            )
        if raw is None:
            raise ReferenceNotFoundError("the worker's session does not exist")
        assignment = raw.get("worker_assignment") or {}
        fence = self._fence
        if fence is not None and (
            assignment.get("writer_epoch") != fence.writer_epoch
            or assignment.get("generation") != fence.generation
        ):
            raise WriterFencedError("the worker assignment was fenced")
        replay = raw.get("state_revision") == session.state_revision
        if replay and raw.get("status") == session.status.value:
            return
        raise RevisionConflictError("stale session revision")
