"""Collections, strict validators, and approved indexes (docs/02 §19, §24; docs/16 §12, §17)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import make_session

from voice_agent.persistence.mongodb.bootstrap import apply_schema, verify_schema
from voice_agent.persistence.mongodb.codecs.bson_codec import model_to_bson
from voice_agent.persistence.mongodb.collection_names import ALL_COLLECTIONS, Collection
from voice_agent.persistence.mongodb.documents.session import session_document
from voice_agent.persistence.mongodb.indexes import INDEXES

pytestmark = pytest.mark.asyncio


async def test_all_fourteen_collections_conform(backend: Backend) -> None:
    report = await verify_schema(backend.database)

    assert report.conforms, report.to_safe_dict()
    assert {item.collection for item in report.collections} == {c.value for c in ALL_COLLECTIONS}
    for item in report.collections:
        assert item.validator_matches
        assert item.missing_indexes == ()
        assert item.mismatched_indexes == ()
        assert item.unapproved_indexes == ()


async def test_indexes_match_approved_names_keys_and_options(backend: Backend) -> None:
    for collection in ALL_COLLECTIONS:
        info = await backend.database[collection.value].index_information()
        for spec in INDEXES[collection]:
            stored = info[spec.name]
            assert [tuple(pair) for pair in stored["key"]] == list(spec.keys)
            assert bool(stored.get("unique")) is spec.unique
            if spec.partial is None:
                assert "partialFilterExpression" not in stored


async def test_reapplying_is_additive_and_idempotent(backend: Backend) -> None:
    report = await apply_schema(backend.database)

    assert report.conforms
    assert report.created_collections == ()
    assert report.updated_validators == ()
    assert report.created_indexes == ()


async def _insert_raw(backend: Backend, document: dict[str, Any]) -> None:
    await backend.database[Collection.VOICE_SESSIONS.value].insert_one(document)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda d: d.pop("correlation_id"), id="missing_required"),
        pytest.param(lambda d: d.update(state_revision="1"), id="wrong_type"),
        pytest.param(lambda d: d.update(status="paused"), id="invalid_enum"),
        pytest.param(lambda d: d.update(api_key="leak"), id="unknown_root_field"),
        pytest.param(lambda d: d.update(session_id="NOT-A-UUID"), id="non_canonical_id"),
        pytest.param(lambda d: d.pop("next_reconcile_at"), id="nonterminal_without_due_time"),
        pytest.param(lambda d: d["transport"].update(room_hint="x"), id="unknown_embedded_field"),
    ],
)
async def test_validator_rejects_invalid_documents(backend: Backend, mutation: Any) -> None:
    from pymongo.errors import WriteError

    config = await backend.config()
    record = backend.track_session(make_session(config))
    document = model_to_bson(session_document(record))
    mutation(document)
    backend.tracker.sessions.add(str(document["session_id"]))

    with pytest.raises(WriteError) as caught:
        await _insert_raw(backend, document)

    assert caught.value.code == 121
    assert (
        await backend.database[Collection.VOICE_SESSIONS.value].count_documents(
            {"client_request_id": record.client_request_id}
        )
        == 0
    )


async def test_validator_accepts_a_complete_valid_document(backend: Backend) -> None:
    config = await backend.config()
    record = backend.track_session(make_session(config))

    await _insert_raw(backend, model_to_bson(session_document(record)))

    stored = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": record.session_id}
    )
    assert stored is not None
    assert stored["event_sequence_counter"] == 0
    assert stored["privacy_policy_version"] == "rd_privacy_v1"
