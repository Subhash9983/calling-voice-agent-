"""``cost_entries`` repository and worker ``CostEntryRepository`` adapter (docs/02 §10).

A calculation run is inserted atomically in one transaction and is then
immutable: a replay of the identical run is a no-op, a different run under
the same ID is rejected, and ``calculation_version`` must increase within a
session/scope target. Session totals use only ``charge`` lines of the latest
successful (``final``) session-scope run, so allocations never double count.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from pymongo import DESCENDING

from voice_agent.contracts.cost import CostCalculation, RateCard
from voice_agent.contracts.enums import CalculationStatus
from voice_agent.costing.cost_entries import CostRunContext, cost_entries_from_calculation
from voice_agent.domain.cost_entry import AggregationBehavior, CostEntryRecord, CostScope
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    encode,
    in_transaction,
    parse,
)
from voice_agent.ports.clock import IdGenerator
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.persistence import PersistenceRejectedError, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

MAX_RUN_LINES: Final = 200
_TARGET_FIELD: Final = {
    CostScope.SESSION: "session_id",
    CostScope.TURN: "turn_id",
    CostScope.OPERATION: "operation_id",
}


def _run_identity(entries: Sequence[CostEntryRecord]) -> CostEntryRecord:
    if not entries or len(entries) > MAX_RUN_LINES:
        raise PersistenceRejectedError("a calculation run needs 1..200 lines")
    first = entries[0]
    keys = {
        (e.calculation_run_id, e.calculation_version, e.session_id, e.scope, e.scope_target_id)
        for e in entries
    }
    if len(keys) != 1:
        raise PersistenceRejectedError("all lines of a run share one identity and scope target")
    return first


class MongoCostEntryStore(MongoRepository):
    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        first = _run_identity(entries)
        existing = await self.list_run(first.calculation_run_id)
        if existing:
            if _encoded(existing) == _encoded(entries):
                return
            raise DuplicateKeyError("calculation run already exists with other lines")
        await self._require_session(first.session_id)
        await self._require_newer_version(first)
        documents = [encode(entry) for entry in entries]

        async def write(session: Any) -> None:
            await self.collection(Collection.COST_ENTRIES).insert_many(
                documents, ordered=True, session=session
            )

        await in_transaction(self._persistence, write)

    async def list_run(self, calculation_run_id: str) -> Sequence[CostEntryRecord]:
        async with translate_errors():
            rows = await (
                self.collection(Collection.COST_ENTRIES)
                .find(
                    {"calculation_run_id": calculation_run_id},
                    sort=[("cost_entry_id", 1)],
                    limit=MAX_RUN_LINES,
                )
                .to_list(length=MAX_RUN_LINES)
            )
        return [parse(CostEntryRecord, row) for row in rows]

    async def latest_final_run(
        self, session_id: str, *, scope: CostScope, target_id: str
    ) -> Sequence[CostEntryRecord]:
        latest = await self._latest(session_id, scope, target_id, final_only=True)
        return [] if latest is None else await self.list_run(latest["calculation_run_id"])

    async def session_charge_total(self, session_id: str) -> Decimal | None:
        lines = await self.latest_final_run(
            session_id, scope=CostScope.SESSION, target_id=session_id
        )
        charges = [
            line.currency_conversion.converted_net_cost
            for line in lines
            if line.aggregation_behavior is AggregationBehavior.CHARGE
        ]
        return sum(charges, Decimal(0)) if charges else None

    async def _latest(
        self, session_id: str, scope: CostScope, target_id: str, *, final_only: bool
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {"session_id": session_id, "scope": scope.value}
        if final_only:
            filters["calculation_status"] = CalculationStatus.FINAL.value
        if scope is not CostScope.SESSION:
            filters[_TARGET_FIELD[scope]] = target_id
        async with translate_errors():
            found: dict[str, Any] | None = await self.collection(Collection.COST_ENTRIES).find_one(
                filters,
                projection={"calculation_run_id": 1, "calculation_version": 1, "_id": 0},
                sort=[("calculation_version", DESCENDING)],
            )
        return found

    async def _require_newer_version(self, entry: CostEntryRecord) -> None:
        latest = await self._latest(
            entry.session_id, entry.scope, entry.scope_target_id, final_only=False
        )
        if latest is not None and latest["calculation_version"] >= entry.calculation_version:
            raise RevisionConflictError("calculation_version must increase within its scope")

    async def _require_session(self, session_id: str) -> None:
        async with translate_errors():
            found = await self.collection(Collection.VOICE_SESSIONS).count_documents(
                {"session_id": session_id}, limit=1
            )
        if not found:
            raise ReferenceNotFoundError("cost entries require an existing session")


def _encoded(entries: Sequence[CostEntryRecord]) -> dict[str, dict[str, Any]]:
    """Storage-normalized lines keyed by ID (timestamps at BSON precision)."""
    return {entry.cost_entry_id: encode(entry) for entry in entries}


class MongoCostEntryRepository:
    """Worker port adapter: persists one calculation as an immutable run."""

    def __init__(
        self,
        persistence: MongoPersistence,
        *,
        context_for: Any,
        card: RateCard,
        ids: IdGenerator,
    ) -> None:
        self._store = MongoCostEntryStore(persistence)
        self._context_for = context_for
        self._card = card
        self._ids = ids

    async def add_calculation(
        self, session_id: str, run_id: str, calculation: CostCalculation
    ) -> None:
        context: CostRunContext = self._context_for(session_id, run_id)
        entries = cost_entries_from_calculation(
            calculation, card=self._card, context=context, ids=self._ids
        )
        if entries:
            await self._store.insert_run(entries)
