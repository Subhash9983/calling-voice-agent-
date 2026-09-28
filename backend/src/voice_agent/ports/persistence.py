"""Durable-store ports for the remaining core collections (docs/02 §5, §10-§13, §18, §20).

Every mutation is a named, revision-checked operation; there is no generic
document update. Queries are the approved bounded patterns only.
Implementations translate store failures into the normalized errors below
and never include document content in them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.consent import ConsentFulfilment, ConsentRecord, ConsentScope
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope
from voice_agent.domain.error_event import (
    ErrorEventRecord,
    ResolutionAction,
    ResolutionStatus,
)
from voice_agent.domain.feedback import FeedbackRecord, ResolutionCode, ReviewStatus

MAX_QUERY_LIMIT = 100


class ReferenceNotFoundError(RuntimeError):
    """A referenced parent record is missing or belongs to another parent."""


class PersistenceRejectedError(RuntimeError):
    """The store rejected a write (validator/limit); normalized ``persistence_failed``."""


class WriterFencedError(RuntimeError):
    """A worker write no longer matches the current writer epoch/generation."""


@runtime_checkable
class AgentConfigRepository(Protocol):
    async def insert(self, config: AgentConfig) -> None:
        """Insert an immutable version; ``DuplicateKeyError`` on ID/version reuse."""
        ...

    async def get(self, agent_config_id: str) -> AgentConfig | None: ...

    async def list_active(self, environment: str) -> Sequence[AgentConfig]: ...

    async def list_versions(self, agent_id: str, *, limit: int) -> Sequence[AgentConfig]: ...

    async def activate(
        self, agent_config_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> AgentConfig:
        """``draft -> active``; ``DuplicateKeyError`` if another version is active."""
        ...

    async def retire(
        self, agent_config_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> AgentConfig: ...

    async def mark_expiry(
        self, agent_config_id: str, *, expected_revision: int, expires_at: datetime, now: datetime
    ) -> AgentConfig: ...


@runtime_checkable
class CostEntryStore(Protocol):
    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        """Insert one immutable calculation run; a replayed identical run is a no-op."""
        ...

    async def list_run(self, calculation_run_id: str) -> Sequence[CostEntryRecord]: ...

    async def latest_final_run(
        self, session_id: str, *, scope: CostScope, target_id: str
    ) -> Sequence[CostEntryRecord]:
        """Lines of the highest-version ``final`` run for one scope target."""
        ...

    async def session_charge_total(self, session_id: str) -> Decimal | None:
        """Reporting-currency total of ``charge`` lines of the latest final session run."""
        ...


@runtime_checkable
class ErrorEventStore(Protocol):
    async def record(self, error: ErrorEventRecord) -> bool:
        """Insert; ``False`` when the same ``error_id`` was already delivered."""
        ...

    async def get(self, error_id: str) -> ErrorEventRecord | None: ...

    async def list_for_session(
        self, session_id: str, *, limit: int, after: datetime | None = None
    ) -> Sequence[ErrorEventRecord]: ...

    async def list_by_fingerprint(
        self, fingerprint: str, *, limit: int
    ) -> Sequence[ErrorEventRecord]: ...

    async def resolution_queue(
        self, environment: str, status: ResolutionStatus, *, limit: int
    ) -> Sequence[ErrorEventRecord]: ...

    async def resolve(
        self,
        error_id: str,
        *,
        expected_revision: int,
        status: ResolutionStatus,
        action: ResolutionAction | None,
        now: datetime,
        note: str | None = None,
    ) -> ErrorEventRecord: ...


@runtime_checkable
class FeedbackStore(Protocol):
    async def list_for_session(
        self, session_id: str, *, limit: int
    ) -> Sequence[FeedbackRecord]: ...

    async def review_queue(
        self, environment: str, status: ReviewStatus, *, limit: int
    ) -> Sequence[FeedbackRecord]: ...

    async def record_review(
        self,
        feedback_id: str,
        *,
        expected_review_revision: int,
        status: ReviewStatus,
        reviewer: str,
        now: datetime,
        resolution_code: ResolutionCode | None = None,
        note: str | None = None,
    ) -> FeedbackRecord: ...


@runtime_checkable
class ConsentRecordStore(Protocol):
    async def insert(self, record: ConsentRecord) -> bool:
        """Insert one immutable decision; ``False`` for an idempotent resubmission."""
        ...

    async def get_by_receipt(self, consent_receipt_id: str) -> ConsentRecord | None: ...

    async def latest_decision(
        self, session_id: str, scope: ConsentScope
    ) -> ConsentRecord | None: ...

    async def list_chain(self, consent_chain_id: str, *, limit: int) -> Sequence[ConsentRecord]: ...

    async def update_fulfilment(
        self, consent_record_id: str, *, expected_revision: int, fulfilment: ConsentFulfilment
    ) -> ConsentRecord: ...

    async def mark_evidence_expiry(
        self, consent_record_id: str, *, expires_at: datetime
    ) -> ConsentRecord: ...


@dataclass(frozen=True, slots=True)
class SessionCleanupReport:
    """Per-session child-first cleanup evidence (counts only, no content)."""

    session_id: str
    candidates: Mapping[str, int]
    deleted: Mapping[str, int] = field(default_factory=dict)
    parent_deleted: bool = False
    failure: str | None = None


@runtime_checkable
class RetentionCleanupStore(Protocol):
    async def select_expired_sessions(
        self, environment: str, *, now: datetime, limit: int
    ) -> Sequence[str]:
        """Terminal sessions whose ``expires_at`` has passed (``ix_sessions_expiry``)."""
        ...

    async def cleanup_session(
        self, session_id: str, *, now: datetime, dry_run: bool
    ) -> SessionCleanupReport:
        """Delete children in the approved order, verify each, then the parent."""
        ...
