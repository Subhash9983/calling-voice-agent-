"""BSON codecs, validator generation, driver-error normalization, and client lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from pydantic import BaseModel, Field, JsonValue, SecretStr
from pymongo.errors import (
    AutoReconnect,
    BulkWriteError,
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)
from tests.support.fake_mongo import FakeClient, FakeDatabase

from voice_agent.contracts.base import CanonicalId, NonNegativeDecimal, UtcDatetime
from voice_agent.persistence.mongodb.client import MongoClientSettings, MongoPersistence
from voice_agent.persistence.mongodb.client.errors import (
    IndexedDuplicateKeyError,
    duplicate_key_fields,
    translate_errors,
)
from voice_agent.persistence.mongodb.codecs.bson_codec import (
    document_from_bson,
    from_bson,
    model_to_bson,
    to_bson,
    truncate_to_millis,
)
from voice_agent.persistence.mongodb.codecs.schema import UUID_PATTERN, model_schema
from voice_agent.ports.control_plane import DuplicateKeyError as PortDuplicateKeyError
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError

pytestmark = pytest.mark.asyncio


class Color(StrEnum):
    RED = "red"


class Inner(BaseModel):
    value: int


class Sample(BaseModel):
    identifier: CanonicalId
    when: UtcDatetime
    amount: NonNegativeDecimal
    color: Color
    kind: Literal["a", "b"]
    flag: bool
    ratio: Annotated[float, Field(gt=0, lt=1)]
    tags: Annotated[tuple[Annotated[str, Field(max_length=5)], ...], Field(max_length=3)] = ()
    unique: frozenset[str] = frozenset()
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    scores: dict[str, Inner] | None = None
    inner: Inner | None = None
    choice: Inner | Color | None = None
    day: date | None = None
    note: Annotated[str, Field(pattern=r"^[a-z]+$")] | None = None


# ------------------------------------------------------------------ codec --
async def test_to_bson_converts_every_supported_type() -> None:
    now = datetime(2026, 9, 28, 1, 2, 3, 456789, tzinfo=UTC)
    value = {
        "enum": Color.RED,
        "decimal": Decimal("1.25"),
        "date": date(2026, 9, 28),
        "tuple": (1, 2),
        "set": frozenset({"b", "a"}),
        "model": Inner(value=1),
        "when": now,
        "float": 0.5,
        "none": None,
        "oid": ObjectId(),
    }

    converted = to_bson(value)

    assert converted["enum"] == "red"
    assert type(converted["enum"]) is str
    assert converted["decimal"] == Decimal128("1.25")
    assert converted["date"] == datetime(2026, 9, 28, tzinfo=UTC)
    assert converted["tuple"] == [1, 2]
    assert converted["set"] == ["a", "b"]
    assert converted["model"] == {"value": 1}
    assert converted["when"].microsecond == 456000


@pytest.mark.parametrize(
    "bad",
    [
        {"$where": 1},
        {"a.b": 1},
        {"": 1},
        float("nan"),
        Decimal("Infinity"),
        Decimal("1." + "1" * 40),
        2**70,
        object(),
        datetime(2026, 1, 1),
    ],
)
async def test_to_bson_rejects_unsafe_values(bad: Any) -> None:
    with pytest.raises(PersistenceRejectedError):
        to_bson(bad)


async def test_from_bson_restores_decimals_and_utc_and_drops_internal_id() -> None:
    naive = datetime(2026, 9, 28, 1, 2, 3)
    stored = {
        "_id": ObjectId(),
        "amount": Decimal128("2.50"),
        "at": naive,
        "items": [{"q": Decimal128("1")}],
        "aware": datetime(2026, 9, 28, tzinfo=UTC),
    }

    converted = document_from_bson(stored)

    assert "_id" not in converted
    assert converted["amount"] == Decimal("2.50")
    assert converted["at"].tzinfo is UTC
    assert converted["items"] == [{"q": Decimal("1")}]
    assert from_bson("text") == "text"


async def test_truncation_keeps_utc_and_milliseconds() -> None:
    offset = datetime(2026, 9, 28, 6, 0, 0, 999999, tzinfo=timezone(timedelta(hours=5)))

    truncated = truncate_to_millis(offset)

    assert truncated.tzinfo is UTC
    assert truncated.hour == 1
    assert truncated.microsecond == 999000


async def test_model_to_bson_omits_unavailable_optional_values() -> None:
    sample = Sample(
        identifier="00000000-0000-4000-8000-000000000001",
        when=datetime(2026, 9, 28, tzinfo=UTC),
        amount=Decimal("1"),
        color=Color.RED,
        kind="a",
        flag=True,
        ratio=0.5,
    )

    document = model_to_bson(sample)

    assert "inner" not in document
    assert "note" not in document
    assert document["payload"] == {}


# ----------------------------------------------------------------- schema --
async def test_generated_schema_maps_types_and_constraints() -> None:
    schema = model_schema(Sample, root=True)
    props = schema["properties"]

    assert schema["additionalProperties"] is False
    assert props["_id"] == {"bsonType": "objectId"}
    assert set(schema["required"]) == {
        "identifier",
        "when",
        "amount",
        "color",
        "kind",
        "flag",
        "ratio",
        "tags",
        "unique",
        "payload",
    }
    assert props["identifier"]["pattern"] == UUID_PATTERN
    assert props["when"] == {"bsonType": "date"}
    assert props["amount"] == {"bsonType": "decimal", "minimum": 0}
    assert props["color"] == {"bsonType": "string", "enum": ["red"]}
    assert props["kind"] == {"bsonType": "string", "enum": ["a", "b"]}
    assert props["ratio"]["exclusiveMinimum"] is True
    assert props["ratio"]["exclusiveMaximum"] is True
    assert props["tags"]["maxItems"] == 3
    assert props["tags"]["items"]["maxLength"] == 5
    assert props["unique"]["uniqueItems"] is True
    assert props["payload"] == {"bsonType": "object"}
    assert props["scores"]["additionalProperties"]["required"] == ["value"]
    assert props["inner"]["additionalProperties"] is False
    assert "anyOf" in props["choice"]
    assert props["day"] == {"bsonType": "date"}
    assert props["note"]["pattern"] == "^[a-z]+$"


async def test_unsupported_annotation_is_refused() -> None:
    class Bad(BaseModel):
        value: complex

    with pytest.raises(TypeError):
        model_schema(Bad)


# ----------------------------------------------------------------- errors --
async def _raise(exc: Exception) -> None:
    async with translate_errors():
        raise exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            DuplicateKeyError("dup", 11000, {"keyPattern": {"event_id": 1}}),
            IndexedDuplicateKeyError,
        ),
        (OperationFailure("validation", 121, {"errmsg": "x"}), PersistenceRejectedError),
        (OperationFailure("denied", 13, {"errmsg": "x"}), StoreUnavailableError),
        (
            BulkWriteError({"writeErrors": [{"code": 11000, "keyPattern": {"a": 1}}]}),
            IndexedDuplicateKeyError,
        ),
        (BulkWriteError({"writeErrors": [{"code": 121}]}), PersistenceRejectedError),
        (BulkWriteError({"writeErrors": [{"code": 2}]}), StoreUnavailableError),
        (BulkWriteError({}), StoreUnavailableError),
        (AutoReconnect("down"), StoreUnavailableError),
        (ServerSelectionTimeoutError("no primary"), StoreUnavailableError),
    ],
)
async def test_driver_errors_are_normalized_without_chaining(
    exc: Exception, expected: type
) -> None:
    with pytest.raises(expected) as caught:
        await _raise(exc)

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    if isinstance(caught.value, IndexedDuplicateKeyError):
        assert isinstance(caught.value, PortDuplicateKeyError)


async def test_duplicate_key_fields_are_names_only() -> None:
    assert duplicate_key_fields(
        {
            "keyPattern": {"session_id": 1, "sequence_number": 1},
            "keyValue": {"session_id": "secret"},
        }
    ) == ("session_id", "sequence_number")
    assert duplicate_key_fields(None) == ()
    assert duplicate_key_fields({"keyPattern": "odd"}) == ()


# ----------------------------------------------------------------- client --
async def test_client_options_are_bounded_and_majority() -> None:
    options = MongoClientSettings().client_options()

    assert options["serverSelectionTimeoutMS"] == 5_000
    assert options["timeoutMS"] == 10_000
    assert options["w"] == "majority"
    assert options["tz_aware"] is True
    assert options["connect"] is False
    assert options["retryWrites"] is True


async def test_only_the_approved_database_can_be_opened() -> None:
    with pytest.raises(ValueError, match="approved"):
        MongoPersistence(SecretStr("mongodb://127.0.0.1:1/"), database_name="other_db")


async def test_open_uses_the_factory_inside_the_loop_and_close_is_idempotent() -> None:
    database = FakeDatabase()
    client = FakeClient(database)
    calls: list[dict[str, Any]] = []

    def factory(uri: str, **options: Any) -> FakeClient:
        calls.append(options)
        return client

    persistence = MongoPersistence(SecretStr("mongodb://127.0.0.1:1/"), client_factory=factory)
    assert persistence.is_open is False
    with pytest.raises(StoreUnavailableError):
        _ = persistence.database
    with pytest.raises(StoreUnavailableError):
        _ = persistence.client

    persistence.open()
    persistence.open()  # no second client
    await persistence.ping()
    await persistence.close()
    await persistence.close()

    assert len(calls) == 1
    assert client.closed is True
    assert persistence.database_name == "voice_agent_rnd"


async def test_ping_failures_become_store_unavailable() -> None:
    database = FakeDatabase()
    database.available = False
    persistence = MongoPersistence.from_handles(FakeClient(database), database)

    with pytest.raises(StoreUnavailableError):
        await persistence.ping()


async def test_ping_timeout_becomes_store_unavailable() -> None:
    database = FakeDatabase()

    async def slow(*_: Any, **__: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {"ok": 1}

    database.command = slow  # type: ignore[method-assign]
    persistence = MongoPersistence.from_handles(FakeClient(database), database)

    with pytest.raises(StoreUnavailableError):
        await persistence.ping(timeout_s=0.01)
