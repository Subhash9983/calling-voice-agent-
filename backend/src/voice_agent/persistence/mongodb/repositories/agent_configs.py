"""``agent_configs`` repository (docs/02 §5, §18-§19).

Versions are immutable: only lifecycle fields change, each through a
``revision``-checked named operation. ``uq_agent_active_environment``
enforces one active version per agent and environment. A retired version
is marked for expiry only when no live session/run references it and the
expiry is no earlier than every referencing record's expiry.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pymongo import DESCENDING, ReturnDocument

from voice_agent.domain.agent_config import AgentConfig, AgentConfigStatus
from voice_agent.domain.errors import DomainRuleError
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import MongoRepository, encode, parse
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, PersistenceRejectedError
from voice_agent.ports.repositories import RevisionConflictError
from voice_agent.privacy_and_retention.expiry import retired_definition_expires_at

MAX_ACTIVE_CONFIGS: Final = 50


class MongoAgentConfigRepository(MongoRepository):
    async def insert(self, config: AgentConfig) -> None:
        if not config.verify_checksum():
            raise PersistenceRejectedError("configuration checksum does not match its content")
        async with translate_errors():
            await self.collection(Collection.AGENT_CONFIGS).insert_one(encode(config))

    async def get(self, agent_config_id: str) -> AgentConfig | None:
        async with translate_errors():
            raw = await self.collection(Collection.AGENT_CONFIGS).find_one(
                {"agent_config_id": agent_config_id}
            )
        return None if raw is None else parse(AgentConfig, raw)

    async def get_active(self, agent_config_id: str) -> AgentConfig | None:
        config = await self.get(agent_config_id)
        return config if config is not None and config.status is AgentConfigStatus.ACTIVE else None

    async def list_active(self, environment: str) -> Sequence[AgentConfig]:
        return await self._list(
            {"environment": environment, "status": AgentConfigStatus.ACTIVE.value},
            sort=[("name", 1), ("agent_config_id", 1)],
            limit=MAX_ACTIVE_CONFIGS,
        )

    async def list_versions(self, agent_id: str, *, limit: int) -> Sequence[AgentConfig]:
        return await self._list(
            {"agent_id": agent_id},
            sort=[("version", DESCENDING)],
            limit=min(limit, MAX_QUERY_LIMIT),
        )

    async def activate(
        self, agent_config_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> AgentConfig:
        return await self._lifecycle(
            agent_config_id,
            expected_revision,
            AgentConfigStatus.DRAFT,
            {
                "status": AgentConfigStatus.ACTIVE.value,
                "activated_at": to_bson(now),
                "activated_by": actor,
                "updated_at": to_bson(now),
            },
        )

    async def retire(
        self, agent_config_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> AgentConfig:
        return await self._lifecycle(
            agent_config_id,
            expected_revision,
            AgentConfigStatus.ACTIVE,
            {
                "status": AgentConfigStatus.RETIRED.value,
                "retired_at": to_bson(now),
                "retired_by": actor,
                "updated_at": to_bson(now),
            },
        )

    async def mark_expiry(
        self, agent_config_id: str, *, expected_revision: int, expires_at: datetime, now: datetime
    ) -> AgentConfig:
        config = await self.get(agent_config_id)
        if config is None or config.retired_at is None:
            raise DomainRuleError("only a retired configuration can be marked for expiry")
        dependents = await self._dependent_expiries(agent_config_id)
        earliest = retired_definition_expires_at(config.retired_at, dependents)
        if expires_at < earliest:
            raise DomainRuleError("configuration expiry precedes a referencing record's expiry")
        return await self._lifecycle(
            agent_config_id,
            expected_revision,
            AgentConfigStatus.RETIRED,
            {"expires_at": to_bson(expires_at), "updated_at": to_bson(now)},
        )

    async def _dependent_expiries(self, agent_config_id: str) -> list[datetime]:
        """Latest expiry of referencing sessions/runs; a live reference blocks expiry."""
        expiries: list[datetime] = []
        references = (
            (Collection.VOICE_SESSIONS, "agent_config_id"),
            (Collection.EVALUATION_RUNS, "configuration_snapshot.agent_config_id"),
        )
        async with translate_errors():
            for collection, field in references:
                live = await self.collection(collection).count_documents(
                    {field: agent_config_id, "expires_at": {"$exists": False}}, limit=1
                )
                if live:
                    raise DomainRuleError("configuration is still referenced by a live record")
                latest = await self.collection(collection).find_one(
                    {field: agent_config_id},
                    projection={"expires_at": 1, "_id": 0},
                    sort=[("expires_at", DESCENDING)],
                )
                if latest is not None and latest.get("expires_at") is not None:
                    expiries.append(latest["expires_at"])
        return expiries

    async def _lifecycle(
        self,
        agent_config_id: str,
        expected_revision: int,
        required_status: AgentConfigStatus,
        changes: dict[str, Any],
    ) -> AgentConfig:
        async with translate_errors():
            stored = await self.collection(Collection.AGENT_CONFIGS).find_one_and_update(
                {
                    "agent_config_id": agent_config_id,
                    "revision": expected_revision,
                    "status": required_status.value,
                },
                {"$set": changes, "$inc": {"revision": 1}},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise RevisionConflictError("configuration revision or status changed")
        return parse(AgentConfig, stored)

    async def _list(
        self, filters: dict[str, Any], *, sort: list[tuple[str, int]], limit: int
    ) -> list[AgentConfig]:
        async with translate_errors():
            rows = await (
                self.collection(Collection.AGENT_CONFIGS)
                .find(filters, sort=sort, limit=limit)
                .to_list(length=limit)
            )
        return [parse(AgentConfig, row) for row in rows]
