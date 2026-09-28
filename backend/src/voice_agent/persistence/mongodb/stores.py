"""Composition helpers: MongoDB-backed control-plane stores and readiness verification.

The control API wires these behind its ports; nothing outside
``persistence`` touches the driver. Readiness checks are read-only and
bounded: ping, schema conformance, and presence of the default
configuration version that sessions must reference.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.persistence.mongodb.bootstrap import SchemaReport, verify_schema
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.feedback import MongoFeedbackRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import MongoSessionTimelineReader
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError

VERIFY_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class MongoControlPlaneStores:
    sessions: MongoSessionRecordRepository
    feedback: MongoFeedbackRepository
    timeline: MongoSessionTimelineReader
    events: MongoSessionEventLog


def mongo_control_plane_stores(
    persistence: MongoPersistence, *, environment: AgentConfigEnvironment, service_version: str
) -> MongoControlPlaneStores:
    context = EventWriteContext(environment=environment, service_version=service_version)
    return MongoControlPlaneStores(
        sessions=MongoSessionRecordRepository(persistence),
        feedback=MongoFeedbackRepository(persistence),
        timeline=MongoSessionTimelineReader(persistence),
        events=MongoSessionEventLog(persistence, context=context),
    )


class PersistenceHealth(StrEnum):
    READY = "ready"
    UNREACHABLE = "unreachable"
    SCHEMA_MISMATCH = "schema_mismatch"
    DEFAULT_CONFIG_MISSING = "default_config_missing"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    health: PersistenceHealth
    schema: SchemaReport | None = None


async def verify_persistence(
    persistence: MongoPersistence,
    *,
    default_config: tuple[str, str] | None,
    timeout_s: float = VERIFY_TIMEOUT_S,
) -> VerificationResult:
    """Ping, verify validators/indexes, and check the default configuration is stored."""
    try:
        async with asyncio.timeout(timeout_s):
            await persistence.ping(timeout_s=timeout_s)
            schema = await verify_schema(persistence.database)
            if not schema.conforms:
                return VerificationResult(PersistenceHealth.SCHEMA_MISMATCH, schema)
            if default_config is not None and not await _config_stored(persistence, default_config):
                return VerificationResult(PersistenceHealth.DEFAULT_CONFIG_MISSING, schema)
    except (TimeoutError, StoreUnavailableError, PersistenceRejectedError):
        return VerificationResult(PersistenceHealth.UNREACHABLE)
    return VerificationResult(PersistenceHealth.READY, schema)


async def _config_stored(persistence: MongoPersistence, config: tuple[str, str]) -> bool:
    agent_config_id, checksum = config
    async with translate_errors():
        count = await persistence.database[Collection.AGENT_CONFIGS.value].count_documents(
            {"agent_config_id": agent_config_id, "config_checksum": checksum}, limit=1
        )
    return count == 1
