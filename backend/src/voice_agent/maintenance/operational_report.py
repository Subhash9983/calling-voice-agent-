"""Bounded local operational report and retention evidence (docs/14 §17 WP11).

Read-only: the newest ``limit`` sessions of one environment (or one named
session) are reconstructed from stored evidence through the same read ports
the control API uses, and summarized as latency, cost, error, coherence, and
finalization evidence. The report is a local R&D artifact (stdout or an
``outputs/`` file); it is not a monitoring service and sends nothing anywhere.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from voice_agent.costing.rate_card import rate_card_by_id
from voice_agent.events_and_latency.evidence_loader import EvidenceSources, load_session_evidence
from voice_agent.events_and_latency.evidence_report import aggregate_dict, session_dict
from voice_agent.events_and_latency.evidence_types import SessionEvidence
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.errors import MongoErrorEventStore
from voice_agent.persistence.mongodb.repositories.retention_evidence import (
    MongoRetentionEvidenceStore,
)
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import MongoSessionTimelineReader
from voice_agent.ports.control_plane import SessionListQuery

DEFAULT_REPORT_SESSIONS: Final = 20
MAX_REPORT_SESSIONS: Final = 100


def _sources(persistence: MongoPersistence) -> EvidenceSources:
    return EvidenceSources(
        sessions=MongoSessionRecordRepository(persistence),
        timeline=MongoSessionTimelineReader(persistence),
        costs=MongoCostEntryStore(persistence),
        errors=MongoErrorEventStore(persistence),
    )


def evidence_sources(persistence: MongoPersistence) -> EvidenceSources:
    """The read ports the control API and reports use (shared with the evaluation CLI)."""
    return _sources(persistence)


async def _session_ids(
    sources: EvidenceSources, environment: str, *, limit: int, session_id: str | None
) -> list[str]:
    if session_id is not None:
        return [session_id]
    bounded = max(1, min(limit, MAX_REPORT_SESSIONS))
    page = await sources.sessions.list_page(
        SessionListQuery(environment=environment, limit=bounded)
    )
    return [record.session_id for record in page]


async def collect_evidence(
    persistence: MongoPersistence,
    environment: str,
    *,
    limit: int = DEFAULT_REPORT_SESSIONS,
    session_id: str | None = None,
) -> list[SessionEvidence]:
    sources = _sources(persistence)
    collected: list[SessionEvidence] = []
    for identifier in await _session_ids(sources, environment, limit=limit, session_id=session_id):
        data = await load_session_evidence(sources, identifier)
        if data is None or data.session.environment.value != environment:
            continue
        card = rate_card_by_id(data.session.cost_rate_card_version)
        collected.append(build_session_evidence(data, card=card))
    return collected


async def operational_report(
    persistence: MongoPersistence,
    environment: str,
    *,
    now: datetime,
    limit: int = DEFAULT_REPORT_SESSIONS,
    session_id: str | None = None,
) -> dict[str, Any]:
    sessions = await collect_evidence(persistence, environment, limit=limit, session_id=session_id)
    single = session_id is not None
    return {
        "generated_at": now.isoformat(),
        "environment": environment,
        "session_limit": 1 if single else max(1, min(limit, MAX_REPORT_SESSIONS)),
        "aggregate": aggregate_dict(sessions),
        "sessions": [session_dict(s, include_timeline=single) for s in sessions],
    }


async def retention_report(
    persistence: MongoPersistence, environment: str, *, now: datetime, sample: int
) -> dict[str, Any]:
    overview = await MongoRetentionEvidenceStore(persistence).overview(
        environment, now=now, sample=sample
    )
    return {"retention": overview.to_safe_dict()}


def write_report_file(path: Path, report: dict[str, Any]) -> None:
    """Write a new local evidence file; an existing file is never overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
