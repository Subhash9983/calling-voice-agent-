"""``cost_entries`` and ``consent_records`` contracts (docs/02 §10, §13)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    make_calculation,
    make_consent,
    make_cost_run,
    new_id,
    rate_card,
    run_context,
)

from voice_agent.contracts.enums import CalculationStatus
from voice_agent.domain.consent import (
    ConsentDecision,
    ConsentFulfilment,
    ConsentScope,
    FulfilmentStatus,
)
from voice_agent.domain.cost_entry import AggregationBehavior, CostScope
from voice_agent.domain.errors import DomainRuleError
from voice_agent.persistence.mongodb.repositories.consent import MongoConsentRecordStore
from voice_agent.persistence.mongodb.repositories.cost_entries import (
    MongoCostEntryRepository,
    MongoCostEntryStore,
)
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.persistence import PersistenceRejectedError, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio


class _Ids:
    def new_id(self) -> str:
        return new_id()


# ----------------------------------------------------------------- costs --
async def test_calculation_run_is_immutable_and_replay_safe(backend: Backend) -> None:
    record = await backend.session()
    store = MongoCostEntryStore(backend.persistence)
    run = make_cost_run(record)

    await store.insert_run(run)
    await store.insert_run(run)  # identical replay: no-op
    changed = tuple(entry.model_copy(update={"cost_entry_id": new_id()}) for entry in run)

    with pytest.raises(DuplicateKeyError):
        await store.insert_run(changed)
    assert list(await store.list_run(run[0].calculation_run_id)) == list(run)


async def test_versions_increase_and_latest_final_run_supplies_totals(backend: Backend) -> None:
    record = await backend.session()
    store = MongoCostEntryStore(backend.persistence)
    first = make_cost_run(record, version=1)
    second = make_cost_run(record, version=2)
    allocation = tuple(
        entry.model_copy(
            update={
                "cost_entry_id": new_id(),
                "aggregation_behavior": AggregationBehavior.ALLOCATION_ONLY,
            }
        )
        for entry in second
    )

    await store.insert_run(first)
    await store.insert_run((*second, *allocation))
    with pytest.raises(RevisionConflictError):
        await store.insert_run(make_cost_run(record, version=2))
    latest = await store.latest_final_run(
        record.session_id, scope=CostScope.SESSION, target_id=record.session_id
    )
    total = await store.session_charge_total(record.session_id)

    assert {entry.calculation_run_id for entry in latest} == {second[0].calculation_run_id}
    assert total == sum(e.currency_conversion.converted_net_cost for e in second)
    assert total == Decimal("0.00125")


async def test_cost_entries_require_a_session_and_consistent_run(backend: Backend) -> None:
    record = await backend.session()
    store = MongoCostEntryStore(backend.persistence)
    orphan = tuple(e.model_copy(update={"session_id": new_id()}) for e in make_cost_run(record))
    mixed = (*make_cost_run(record), *make_cost_run(record))

    with pytest.raises(ReferenceNotFoundError):
        await store.insert_run(orphan)
    with pytest.raises(PersistenceRejectedError):
        await store.insert_run(mixed)
    with pytest.raises(PersistenceRejectedError):
        await store.insert_run(())
    assert await store.session_charge_total(record.session_id) is None


async def test_worker_adapter_persists_a_calculation_run(backend: Backend) -> None:
    record = await backend.session()
    run_id = new_id()
    adapter = MongoCostEntryRepository(
        backend.persistence,
        context_for=lambda session_id, rid: run_context(record, run_id=rid),
        card=rate_card(),
        ids=_Ids(),
    )

    await adapter.add_calculation(record.session_id, run_id, make_calculation())
    stored = await MongoCostEntryStore(backend.persistence).list_run(run_id)

    assert len(stored) == 1
    assert stored[0].calculation_status is CalculationStatus.FINAL
    assert stored[0].quantity.native_quantity == Decimal("12.5")


# --------------------------------------------------------------- consent --
async def test_consent_decision_is_idempotent_and_immutable(backend: Backend) -> None:
    record = await backend.session()
    consents = MongoConsentRecordStore(backend.persistence)
    grant = make_consent(record)

    assert await consents.insert(grant) is True
    assert await consents.insert(grant) is False
    changed = make_consent(record).model_copy(
        update={"client_submission_id": grant.client_submission_id}
    )
    with pytest.raises(PersistenceRejectedError):
        await consents.insert(changed)  # checksum no longer matches the evidence
    assert await consents.get_by_receipt(grant.consent_receipt_id) == grant


async def test_consent_chain_and_latest_decision(backend: Backend) -> None:
    record = await backend.session()
    consents = MongoConsentRecordStore(backend.persistence)
    grant = make_consent(record)
    await consents.insert(grant)
    revoke = make_consent(
        record,
        decision=ConsentDecision.REVOKED,
        chain_id=grant.consent_chain_id,
        supersedes=grant.consent_record_id,
        decision_at=grant.decision_at + timedelta(seconds=5),
    )
    await consents.insert(revoke)
    stray = make_consent(
        record, decision=ConsentDecision.REVOKED, supersedes=grant.consent_record_id
    )

    latest = await consents.latest_decision(record.session_id, ConsentScope.RECORD_USER_AUDIO)
    chain = await consents.list_chain(grant.consent_chain_id, limit=10)

    assert latest is not None
    assert latest.decision is ConsentDecision.REVOKED
    assert [c.consent_record_id for c in chain] == [
        revoke.consent_record_id,
        grant.consent_record_id,
    ]
    with pytest.raises(DomainRuleError):
        await consents.insert(stray)


async def test_fulfilment_and_evidence_expiry(backend: Backend) -> None:
    record = await backend.session()
    consents = MongoConsentRecordStore(backend.persistence)
    grant = make_consent(record)
    await consents.insert(grant)
    pending = ConsentFulfilment(status=FulfilmentStatus.PENDING, revision=1)

    updated = await consents.update_fulfilment(
        grant.consent_record_id, expected_revision=0, fulfilment=pending
    )
    with pytest.raises(RevisionConflictError):
        await consents.update_fulfilment(
            grant.consent_record_id, expected_revision=0, fulfilment=pending
        )
    with pytest.raises(DomainRuleError):
        await consents.mark_evidence_expiry(
            grant.consent_record_id, expires_at=grant.decision_at + timedelta(days=30)
        )
    done = await consents.update_fulfilment(
        grant.consent_record_id,
        expected_revision=1,
        fulfilment=ConsentFulfilment(status=FulfilmentStatus.COMPLETED, revision=2),
    )
    marked = await consents.mark_evidence_expiry(
        grant.consent_record_id, expires_at=grant.decision_at + timedelta(days=30)
    )

    assert updated.fulfilment.status is FulfilmentStatus.PENDING
    assert done.fulfilment.revision == 2
    assert marked.expires_at == grant.decision_at + timedelta(days=30)
    assert marked.verify_checksum()


async def test_consent_requires_an_existing_session(backend: Backend) -> None:
    record = await backend.session()
    orphan = make_consent(record.model_copy(update={"session_id": new_id()}))

    with pytest.raises(ReferenceNotFoundError):
        await MongoConsentRecordStore(backend.persistence).insert(orphan)
