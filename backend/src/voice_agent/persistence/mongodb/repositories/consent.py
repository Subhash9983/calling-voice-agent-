"""``consent_records`` repository (docs/02 §13, §18-§19).

Decisions are immutable and append-only per chain; a resubmission with the
same ``client_submission_id`` and identical evidence is idempotent. Only the
``fulfilment`` section changes (``fulfilment.revision`` compare-and-set),
and evidence expiry is set only after covered-asset deletion is verified.
Consent evidence is never removed by ordinary session cleanup.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pymongo import DESCENDING, ReturnDocument

from voice_agent.domain.consent import ConsentFulfilment, ConsentRecord, ConsentScope
from voice_agent.domain.errors import DomainRuleError
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError, translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    encode,
    parse,
)
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.persistence import (
    MAX_QUERY_LIMIT,
    PersistenceRejectedError,
    ReferenceNotFoundError,
)
from voice_agent.ports.repositories import RevisionConflictError


class MongoConsentRecordStore(MongoRepository):
    async def insert(self, record: ConsentRecord) -> bool:
        if not record.verify_checksum():
            raise PersistenceRejectedError("consent record checksum does not match its evidence")
        await self._check_references(record)
        try:
            async with translate_errors():
                await self.collection(Collection.CONSENT_RECORDS).insert_one(encode(record))
        except IndexedDuplicateKeyError:
            existing = await self._find_one({"client_submission_id": record.client_submission_id})
            if existing is not None and existing.record_checksum == record.record_checksum:
                return False
            raise DuplicateKeyError("consent submission reused with different evidence") from None
        return True

    async def get_by_receipt(self, consent_receipt_id: str) -> ConsentRecord | None:
        return await self._find_one({"consent_receipt_id": consent_receipt_id})

    async def get(self, consent_record_id: str) -> ConsentRecord | None:
        return await self._find_one({"consent_record_id": consent_record_id})

    async def latest_decision(self, session_id: str, scope: ConsentScope) -> ConsentRecord | None:
        async with translate_errors():
            raw = await self.collection(Collection.CONSENT_RECORDS).find_one(
                {"session_id": session_id, "scope": scope.value},
                sort=[("decision_at", DESCENDING)],
            )
        return None if raw is None else parse(ConsentRecord, raw)

    async def list_chain(self, consent_chain_id: str, *, limit: int) -> Sequence[ConsentRecord]:
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        async with translate_errors():
            rows = await (
                self.collection(Collection.CONSENT_RECORDS)
                .find(
                    {"consent_chain_id": consent_chain_id},
                    sort=[("decision_at", DESCENDING)],
                    limit=bounded,
                )
                .to_list(length=bounded)
            )
        return [parse(ConsentRecord, row) for row in rows]

    async def update_fulfilment(
        self, consent_record_id: str, *, expected_revision: int, fulfilment: ConsentFulfilment
    ) -> ConsentRecord:
        current = await self.get(consent_record_id)
        if current is None or current.fulfilment.revision != expected_revision:
            raise RevisionConflictError("consent fulfilment revision changed")
        updated = current.with_fulfilment(fulfilment)
        async with translate_errors():
            stored = await self.collection(Collection.CONSENT_RECORDS).find_one_and_update(
                {"consent_record_id": consent_record_id, "fulfilment.revision": expected_revision},
                {"$set": {"fulfilment": encode(updated.fulfilment)}},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise RevisionConflictError("consent fulfilment revision changed")
        return parse(ConsentRecord, stored)

    async def mark_evidence_expiry(
        self, consent_record_id: str, *, expires_at: datetime
    ) -> ConsentRecord:
        current = await self.get(consent_record_id)
        if current is None:
            raise ReferenceNotFoundError("consent record not found")
        marked = current.with_evidence_expiry(expires_at)
        async with translate_errors():
            stored = await self.collection(Collection.CONSENT_RECORDS).find_one_and_update(
                {
                    "consent_record_id": consent_record_id,
                    "fulfilment.revision": current.fulfilment.revision,
                },
                {"$set": {"expires_at": to_bson(marked.expires_at)}},
                return_document=ReturnDocument.AFTER,
            )
        if stored is None:
            raise RevisionConflictError("consent fulfilment changed while marking expiry")
        return parse(ConsentRecord, stored)

    async def _check_references(self, record: ConsentRecord) -> None:
        async with translate_errors():
            session = await self.collection(Collection.VOICE_SESSIONS).count_documents(
                {"session_id": record.session_id}, limit=1
            )
        if not session:
            raise ReferenceNotFoundError("consent requires an existing session")
        previous_id = record.supersedes_consent_record_id
        if previous_id is None:
            return
        previous = await self.get(previous_id)
        if previous is None or (
            previous.consent_chain_id,
            previous.session_id,
            previous.scope,
        ) != (record.consent_chain_id, record.session_id, record.scope):
            raise DomainRuleError("a later decision must continue the same chain and scope")

    async def _find_one(self, filters: dict[str, str]) -> ConsentRecord | None:
        async with translate_errors():
            raw = await self.collection(Collection.CONSENT_RECORDS).find_one(filters)
        return None if raw is None else parse(ConsentRecord, raw)
