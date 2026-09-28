"""Additive schema bootstrap and conformance verification (docs/02 §19, §24; docs/16 §17).

``apply_schema`` creates missing collections with their strict validators,
updates validators on existing ones (``collMod``), and creates missing
approved indexes. It is additive only: it never drops a collection, index,
or document, never lists other databases, and reports (without changing)
an existing index whose definition differs from the approved one.
``verify_schema`` is the read-only readiness check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pymongo import IndexModel

from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import from_bson
from voice_agent.persistence.mongodb.collection_names import ALL_COLLECTIONS, Collection
from voice_agent.persistence.mongodb.indexes import INDEXES, IndexSpec
from voice_agent.persistence.mongodb.validators import (
    VALIDATION_ACTION,
    VALIDATION_LEVEL,
    collection_options,
    validator_for,
)

MONGO_ID_INDEX = "_id_"
MAX_LISTED_COLLECTIONS = 200


@dataclass(frozen=True, slots=True)
class CollectionReport:
    collection: str
    exists: bool
    validator_matches: bool
    missing_indexes: tuple[str, ...] = ()
    mismatched_indexes: tuple[str, ...] = ()
    unapproved_indexes: tuple[str, ...] = ()

    @property
    def conforms(self) -> bool:
        return (
            self.exists
            and self.validator_matches
            and not self.missing_indexes
            and not self.mismatched_indexes
        )


@dataclass(frozen=True, slots=True)
class SchemaReport:
    collections: tuple[CollectionReport, ...]
    created_collections: tuple[str, ...] = ()
    updated_validators: tuple[str, ...] = ()
    created_indexes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conforms(self) -> bool:
        return all(item.conforms for item in self.collections)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "conforms": self.conforms,
            "created_collections": list(self.created_collections),
            "updated_validators": list(self.updated_validators),
            "created_indexes": list(self.created_indexes),
            "collections": [
                {
                    "collection": item.collection,
                    "exists": item.exists,
                    "validator_matches": item.validator_matches,
                    "missing_indexes": list(item.missing_indexes),
                    "mismatched_indexes": list(item.mismatched_indexes),
                    "unapproved_indexes": list(item.unapproved_indexes),
                }
                for item in self.collections
            ],
        }


def _expected_index(spec: IndexSpec) -> dict[str, Any]:
    expected: dict[str, Any] = {"key": [list(pair) for pair in spec.keys]}
    if spec.unique:
        expected["unique"] = True
    if spec.partial is not None:
        expected["partialFilterExpression"] = dict(spec.partial)
    return expected


def _stored_index(info: Mapping[str, Any]) -> dict[str, Any]:
    stored: dict[str, Any] = {"key": [[name, int(direction)] for name, direction in info["key"]]}
    if info.get("unique"):
        stored["unique"] = True
    if "partialFilterExpression" in info:
        stored["partialFilterExpression"] = from_bson(dict(info["partialFilterExpression"]))
    return stored


async def _collection_options(database: Any) -> dict[str, Mapping[str, Any]]:
    """Options of the approved collections in the approved database only.

    Atlas Flex rejects non-regex ``listCollections`` name filters, so the
    single approved database is listed (bounded) and filtered here.
    """
    names = {collection.value for collection in ALL_COLLECTIONS}
    async with translate_errors():
        cursor = await database.list_collections()
        rows = await cursor.to_list(length=MAX_LISTED_COLLECTIONS)
    return {row["name"]: row.get("options", {}) for row in rows if row["name"] in names}


def _validator_matches(options: Mapping[str, Any], collection: Collection) -> bool:
    return (
        from_bson(options.get("validator")) == validator_for(collection)
        and options.get("validationLevel") == VALIDATION_LEVEL
        and options.get("validationAction") == VALIDATION_ACTION
    )


async def _index_report(
    database: Any, collection: Collection
) -> tuple[list[str], list[str], list[str]]:
    async with translate_errors():
        info = await database[collection.value].index_information()
    missing, mismatched = [], []
    for spec in INDEXES[collection]:
        stored = info.get(spec.name)
        if stored is None:
            missing.append(spec.name)
        elif _stored_index(stored) != _expected_index(spec):
            mismatched.append(spec.name)
    approved = {spec.name for spec in INDEXES[collection]} | {MONGO_ID_INDEX}
    unapproved = sorted(name for name in info if name not in approved)
    return missing, mismatched, unapproved


async def verify_schema(database: Any) -> SchemaReport:
    """Read-only conformance: collections, strict validators, and approved indexes."""
    options = await _collection_options(database)
    reports = []
    for collection in ALL_COLLECTIONS:
        if collection.value not in options:
            reports.append(
                CollectionReport(collection.value, exists=False, validator_matches=False)
            )
            continue
        missing, mismatched, unapproved = await _index_report(database, collection)
        reports.append(
            CollectionReport(
                collection.value,
                exists=True,
                validator_matches=_validator_matches(options[collection.value], collection),
                missing_indexes=tuple(missing),
                mismatched_indexes=tuple(mismatched),
                unapproved_indexes=tuple(unapproved),
            )
        )
    return SchemaReport(collections=tuple(reports))


async def apply_schema(
    database: Any, *, collections: Sequence[Collection] = ALL_COLLECTIONS
) -> SchemaReport:
    """Additively provision the approved collections, validators, and indexes."""
    existing = await _collection_options(database)
    created, updated, indexes = [], [], []
    for collection in collections:
        options = collection_options(collection)
        async with translate_errors():
            if collection.value not in existing:
                await database.create_collection(collection.value, **options)
                created.append(collection.value)
            elif not _validator_matches(existing[collection.value], collection):
                await database.command({"collMod": collection.value, **options})
                updated.append(collection.value)
        indexes.extend(await _create_missing_indexes(database, collection))
    report = await verify_schema(database)
    return SchemaReport(
        collections=report.collections,
        created_collections=tuple(created),
        updated_validators=tuple(updated),
        created_indexes=tuple(indexes),
    )


async def _create_missing_indexes(database: Any, collection: Collection) -> list[str]:
    missing, _mismatched, _unapproved = await _index_report(database, collection)
    specs = [spec for spec in INDEXES[collection] if spec.name in missing]
    if not specs:
        return []
    models = [IndexModel(list(spec.keys), **spec.options()) for spec in specs]
    async with translate_errors():
        await database[collection.value].create_indexes(models)
    return [f"{collection.value}.{spec.name}" for spec in specs]
