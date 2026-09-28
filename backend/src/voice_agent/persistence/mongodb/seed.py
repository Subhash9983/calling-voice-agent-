"""Idempotent seeding of approved non-secret agent configurations (docs/03 §23).

A configuration version is inserted only if absent; an existing version
with a different checksum is reported, never overwritten (versions are
immutable, docs/02 §5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository


class SeedOutcome(StrEnum):
    INSERTED = "inserted"
    PRESENT = "present"
    CHECKSUM_MISMATCH = "checksum_mismatch"


@dataclass(frozen=True, slots=True)
class SeedResult:
    agent_config_id: str
    outcome: SeedOutcome


async def seed_agent_configs(
    persistence: MongoPersistence, documents: Iterable[Mapping[str, Any]]
) -> tuple[SeedResult, ...]:
    repository = MongoAgentConfigRepository(persistence)
    results = []
    for document in documents:
        config = AgentConfig.model_validate(document)
        results.append(SeedResult(config.agent_config_id, await _seed_one(repository, config)))
    return tuple(results)


async def _seed_one(repository: MongoAgentConfigRepository, config: AgentConfig) -> SeedOutcome:
    existing = await repository.get(config.agent_config_id)
    if existing is not None:
        return _compare(existing, config)
    try:
        await repository.insert(config)
    except IndexedDuplicateKeyError:
        stored = await repository.get(config.agent_config_id)
        return SeedOutcome.CHECKSUM_MISMATCH if stored is None else _compare(stored, config)
    return SeedOutcome.INSERTED


def _compare(existing: AgentConfig, config: AgentConfig) -> SeedOutcome:
    if existing.config_checksum == config.config_checksum:
        return SeedOutcome.PRESENT
    return SeedOutcome.CHECKSUM_MISMATCH
