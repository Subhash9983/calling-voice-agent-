"""In-process fake of the PyMongo Async subset used by the repositories (tests only).

Implements query operators, operator and aggregation-pipeline updates,
positional ``$`` updates, ``$jsonSchema`` validation (the subset the
validators emit), unique/partial indexes, projections, sorting, snapshot
transactions, and fault injection. Errors are real ``pymongo.errors``
instances so the repositories' normalization is exercised unchanged.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import (
    BulkWriteError,
    CollectionInvalid,
    DuplicateKeyError,
    OperationFailure,
    WriteError,
)
from pymongo.results import DeleteResult, InsertManyResult, InsertOneResult, UpdateResult

MISSING: Any = type("Missing", (), {"__repr__": lambda self: "MISSING"})()
REMOVE: Any = type("Remove", (), {"__repr__": lambda self: "REMOVE"})()
INT32 = 2**31


# --------------------------------------------------------------- values --
def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    return None


_TYPE_ORDER = {
    "null": 0,
    "number": 1,
    "string": 2,
    "object": 3,
    "array": 4,
    "objectId": 5,
    "bool": 6,
    "date": 7,
}


def _kind(value: Any) -> str:
    if value is None or value is MISSING:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if _number(value) is not None:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, datetime):
        return "date"
    raise TypeError(f"unsupported fake value {type(value)!r}")


def compare(a: Any, b: Any) -> int:
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        return (_TYPE_ORDER[ka] > _TYPE_ORDER[kb]) - (_TYPE_ORDER[ka] < _TYPE_ORDER[kb])
    if ka == "null":
        return 0
    if ka == "number":
        na, nb = _number(a), _number(b)
        return (na > nb) - (na < nb)  # type: ignore[operator]
    if ka in ("string", "bool", "date", "objectId"):
        return (a > b) - (a < b)
    if ka == "array":
        for x, y in zip(a, b, strict=False):
            c = compare(x, y)
            if c:
                return c
        return (len(a) > len(b)) - (len(a) < len(b))
    return compare(sorted(a.items()), sorted(b.items()))  # pragma: no cover


def equal(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b, strict=True))
    if _kind(a) != _kind(b):
        return False
    return compare(a, b) == 0


def get_single(doc: Any, path: str) -> Any:
    current = doc
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, MISSING)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else MISSING
        else:
            return MISSING
        if current is MISSING:
            return MISSING
    return current


def get_candidates(doc: Any, path: str) -> list[Any]:
    """Values addressed by ``path`` with MongoDB array traversal."""
    parts = path.split(".")

    def walk(value: Any, index: int) -> list[Any]:
        if index == len(parts):
            return [value, *value] if isinstance(value, list) else [value]
        if isinstance(value, list):
            part = parts[index]
            if part.isdigit():
                position = int(part)
                return walk(value[position], index + 1) if position < len(value) else [MISSING]
            found = [walk(item, index) for item in value if isinstance(item, dict)]
            flat = [v for group in found for v in group]
            return flat or [MISSING]
        if isinstance(value, dict):
            return walk(value.get(parts[index], MISSING), index + 1)
        return [MISSING]

    return walk(doc, 0)


def set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = doc
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
            continue
        nxt = current.get(part)
        if not isinstance(nxt, dict | list):
            nxt = {}
            current[part] = nxt
        current = nxt
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    elif value is REMOVE:
        current.pop(last, None)
    else:
        current[last] = value


def unset_path(doc: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    current: Any = doc
    for part in parts[:-1]:
        current = current.get(part) if isinstance(current, dict) else None
        if current is None:
            return
    if isinstance(current, dict):
        current.pop(parts[-1], None)


# ------------------------------------------------------------ expressions --
def evaluate(doc: dict[str, Any], expr: Any, now: datetime) -> Any:
    if isinstance(expr, str):
        if expr == "$$NOW":
            return now
        if expr == "$$REMOVE":
            return REMOVE
        if expr.startswith("$"):
            return get_single(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [evaluate(doc, item, now) for item in expr]
    if not isinstance(expr, dict):
        return expr
    if len(expr) == 1 and next(iter(expr)).startswith("$"):
        operator, args = next(iter(expr.items()))
        return _operator(doc, operator, args, now)
    result = {}
    for key, value in expr.items():
        evaluated = evaluate(doc, value, now)
        if evaluated is not MISSING and evaluated is not REMOVE:
            result[key] = evaluated
    return result


def _present(value: Any) -> bool:
    return value is not MISSING and value is not None


def _operator(doc: dict[str, Any], operator: str, args: Any, now: datetime) -> Any:
    if operator == "$literal":
        return copy.deepcopy(args)
    values = [evaluate(doc, arg, now) for arg in args] if isinstance(args, list) else None
    if operator in ("$min", "$max"):
        present = [v for v in (values or []) if _present(v)]
        if not present:
            return None
        chooser = min if operator == "$min" else max
        return chooser(present, key=_SortKey)
    if operator == "$add":
        assert values is not None
        if any(not _present(v) for v in values):
            return None
        dates = [v for v in values if isinstance(v, datetime)]
        numbers = sum(int(v) for v in values if not isinstance(v, datetime))
        return dates[0] + timedelta(milliseconds=numbers) if dates else numbers
    if operator == "$ifNull":
        assert values is not None
        return next((v for v in values if _present(v)), values[-1])
    if operator == "$cond":
        if isinstance(args, dict):
            args = [args["if"], args["then"], args["else"]]
        test = evaluate(doc, args[0], now)
        return evaluate(doc, args[1] if test else args[2], now)
    if operator == "$in":
        assert values is not None
        return any(equal(values[0], item) for item in values[1])
    comparisons = {
        "$eq": lambda c: c == 0,
        "$ne": lambda c: c != 0,
        "$gt": lambda c: c > 0,
        "$gte": lambda c: c >= 0,
        "$lt": lambda c: c < 0,
        "$lte": lambda c: c <= 0,
    }
    if operator in comparisons:
        assert values is not None
        left = None if values[0] is MISSING else values[0]
        right = None if values[1] is MISSING else values[1]
        return comparisons[operator](compare(left, right))
    raise NotImplementedError(f"fake expression operator {operator}")


class _SortKey:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: _SortKey) -> bool:
        return compare(self.value, other.value) < 0


# ---------------------------------------------------------------- filters --
@dataclass
class MatchContext:
    now: datetime
    positional: dict[str, int] = field(default_factory=dict)


def matches(doc: dict[str, Any], flt: Mapping[str, Any], ctx: MatchContext) -> bool:
    for key, condition in flt.items():
        if key == "$and":
            if not all(matches(doc, sub, ctx) for sub in condition):
                return False
        elif key == "$or":
            if not any(matches(doc, sub, ctx) for sub in condition):
                return False
        elif key == "$nor":
            if any(matches(doc, sub, ctx) for sub in condition):
                return False
        elif key == "$expr":
            if not evaluate(doc, condition, ctx.now):
                return False
        elif not _field_matches(doc, key, condition, ctx):
            return False
    return True


def _is_operator_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(k.startswith("$") for k in value)


def _field_matches(doc: dict[str, Any], path: str, condition: Any, ctx: MatchContext) -> bool:
    candidates = get_candidates(doc, path)
    if not _is_operator_dict(condition):
        return _eq(candidates, condition)
    return all(_op_matches(doc, path, op, arg, candidates, ctx) for op, arg in condition.items())


def _eq(candidates: list[Any], value: Any) -> bool:
    if value is None:
        return any(c is MISSING or c is None for c in candidates)
    return any(c is not MISSING and equal(c, value) for c in candidates)


def _op_matches(
    doc: dict[str, Any], path: str, op: str, arg: Any, candidates: list[Any], ctx: MatchContext
) -> bool:
    present = [c for c in candidates if c is not MISSING]
    if op == "$eq":
        return _eq(candidates, arg)
    if op == "$ne":
        return not _eq(candidates, arg)
    if op == "$in":
        return any(_eq(candidates, item) for item in arg)
    if op == "$nin":
        return not any(_eq(candidates, item) for item in arg)
    if op == "$exists":
        return bool(present) == bool(arg)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        tests = {
            "$gt": lambda c: c > 0,
            "$gte": lambda c: c >= 0,
            "$lt": lambda c: c < 0,
            "$lte": lambda c: c <= 0,
        }
        return any(_kind(c) == _kind(arg) and tests[op](compare(c, arg)) for c in present)
    if op == "$elemMatch":
        array = get_single(doc, path)
        if not isinstance(array, list):
            return False
        for index, item in enumerate(array):
            if isinstance(item, dict) and matches(item, arg, ctx):
                ctx.positional.setdefault(path, index)
                return True
        return False
    if op == "$not":
        return not all(
            _op_matches(doc, path, inner, value, candidates, ctx) for inner, value in arg.items()
        )
    raise NotImplementedError(f"fake query operator {op}")


# ------------------------------------------------------------ validation --
def bson_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if -INT32 <= value < INT32 else "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "date"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, ObjectId):
        return "objectId"
    raise TypeError(f"value is not BSON-encodable in the fake: {type(value)!r}")


def schema_valid(value: Any, schema: Mapping[str, Any]) -> bool:
    if "bsonType" in schema:
        allowed = schema["bsonType"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if bson_type(value) not in allowed:
            return False
    if "enum" in schema and not any(equal(value, item) for item in schema["enum"]):
        return False
    if "anyOf" in schema and not any(schema_valid(value, sub) for sub in schema["anyOf"]):
        return False
    if isinstance(value, dict):
        if any(name not in value for name in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                if not schema_valid(item, properties[name]):
                    return False
            elif extra is False or (isinstance(extra, dict) and not schema_valid(item, extra)):
                return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1e18):
            return False
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return False
    number = _number(value)
    if number is not None:
        if "minimum" in schema:
            low = Decimal(str(schema["minimum"]))
            if number < low or (schema.get("exclusiveMinimum") and number == low):
                return False
        if "maximum" in schema:
            high = Decimal(str(schema["maximum"]))
            if number > high or (schema.get("exclusiveMaximum") and number == high):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1e18):
            return False
        if "items" in schema and not all(schema_valid(item, schema["items"]) for item in value):
            return False
        if schema.get("uniqueItems"):
            for i, a in enumerate(value):
                if any(equal(a, b) for b in value[i + 1 :]):
                    return False
    return True


# ----------------------------------------------------------------- updates --
def apply_update(
    doc: dict[str, Any], update: Any, ctx: MatchContext, *, inserting: bool
) -> dict[str, Any]:
    result = copy.deepcopy(doc)
    if isinstance(update, list):
        for stage in update:
            ((name, spec),) = stage.items()
            if name in ("$set", "$addFields"):
                for path, expr in spec.items():
                    value = evaluate(result, expr, ctx.now)
                    existing = get_single(result, path)
                    is_object_expr = isinstance(expr, dict) and not _is_operator_dict(expr)
                    if is_object_expr and isinstance(existing, dict) and isinstance(value, dict):
                        value = {**existing, **value}
                    if value is MISSING:
                        value = REMOVE
                    set_path(result, path, value)
            elif name in ("$unset", "$project"):
                for path in [spec] if isinstance(spec, str) else spec:
                    unset_path(result, path)
            else:
                raise NotImplementedError(f"fake pipeline stage {name}")
        return result
    for operator, spec in update.items():
        for path, value in spec.items():
            resolved = _positional(path, ctx)
            if operator == "$set" or (operator == "$setOnInsert" and inserting):
                set_path(result, resolved, copy.deepcopy(value))
            elif operator == "$unset":
                unset_path(result, resolved)
            elif operator == "$inc":
                current = get_single(result, resolved)
                set_path(result, resolved, (0 if current is MISSING else current) + value)
            elif operator == "$push":
                _push(result, resolved, value)
            elif operator != "$setOnInsert":
                raise NotImplementedError(f"fake update operator {operator}")
    return result


def _positional(path: str, ctx: MatchContext) -> str:
    if ".$." not in path and not path.endswith(".$"):
        return path
    prefix = path.split(".$", 1)[0]
    index = ctx.positional.get(prefix)
    if index is None:
        raise OperationFailure("The positional operator did not find the match needed", 2)
    return path.replace(".$", f".{index}", 1)


def _push(doc: dict[str, Any], path: str, spec: Any) -> None:
    current = get_single(doc, path)
    array = [] if current is MISSING else list(current)
    if isinstance(spec, dict) and "$each" in spec:
        array.extend(copy.deepcopy(spec["$each"]))
        if "$sort" in spec:
            ((key, direction),) = spec["$sort"].items()
            array.sort(key=lambda item: _SortKey(get_single(item, key)), reverse=direction < 0)
        if "$slice" in spec:
            size = spec["$slice"]
            array = array[size:] if size < 0 else array[:size]
    else:
        array.append(copy.deepcopy(spec))
    set_path(doc, path, array)


# ------------------------------------------------------------- projection --
def project(doc: dict[str, Any], projection: Mapping[str, Any] | None) -> dict[str, Any]:
    if not projection:
        return copy.deepcopy(doc)
    include = {k for k, v in projection.items() if v and k != "_id"}
    if not include:
        result = copy.deepcopy(doc)
        for key, value in projection.items():
            if not value:
                unset_path(result, key)
        return result
    result: dict[str, Any] = {}
    if projection.get("_id", 1) and "_id" in doc:
        result["_id"] = doc["_id"]
    for path in include:
        value = get_single(doc, path)
        if value is not MISSING:
            set_path(result, path, copy.deepcopy(value))
    return result


def sort_documents(docs: list[dict[str, Any]], sort: Iterable[tuple[str, int]] | None) -> None:
    for key, direction in reversed(list(sort or [])):
        docs.sort(key=lambda d, k=key: _SortKey(get_single(d, k)), reverse=direction < 0)


# ------------------------------------------------------------- collection --
@dataclass
class IndexDef:
    name: str
    keys: list[tuple[str, int]]
    unique: bool
    partial: dict[str, Any] | None


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return self._docs if length is None else self._docs[:length]


Fault = Callable[[], Exception]


class FakeCollection:
    def __init__(self, database: FakeDatabase, name: str) -> None:
        self.database = database
        self.name = name
        self.docs: list[dict[str, Any]] = []
        self.options: dict[str, Any] = {}
        self.indexes: dict[str, IndexDef] = {}
        self.operations = 0

    # -- internals --
    def _ctx(self) -> MatchContext:
        return MatchContext(now=self.database.now)

    def _enter(self, method: str) -> None:
        self.operations += 1
        self.database.operations += 1
        self.database.raise_fault(self.name, method)

    def _validate(self, doc: dict[str, Any]) -> None:
        for value in _walk_values(doc):
            bson_type(value)
        validator = self.options.get("validator")
        if validator and not schema_valid(doc, validator["$jsonSchema"]):
            raise WriteError("Document failed validation", 121, {"errInfo": {}})

    def _check_unique(self, doc: dict[str, Any], ignore: dict[str, Any] | None) -> None:
        for index in self.indexes.values():
            if not index.unique:
                continue
            if index.partial and not matches(doc, index.partial, self._ctx()):
                continue
            key = [get_single(doc, name) for name, _ in index.keys]
            for other in self.docs:
                if other is ignore:
                    continue
                if index.partial and not matches(other, index.partial, self._ctx()):
                    continue
                other_key = [get_single(other, name) for name, _ in index.keys]
                if all(
                    equal(_nullable(a), _nullable(b)) for a, b in zip(key, other_key, strict=True)
                ):
                    pattern = {name: direction for name, direction in index.keys}
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error index: {index.name}",
                        11000,
                        {"keyPattern": pattern, "index": index.name},
                    )

    def _store(self, new: dict[str, Any], old: dict[str, Any] | None) -> None:
        self._validate(new)
        self._check_unique(new, old)
        if old is None:
            self.docs.append(new)
        else:
            self.docs[self.docs.index(old)] = new

    def _matching(self, flt: Mapping[str, Any]) -> list[tuple[dict[str, Any], MatchContext]]:
        found = []
        for doc in self.docs:
            ctx = self._ctx()
            if matches(doc, flt, ctx):
                found.append((doc, ctx))
        return found

    def _first(
        self, flt: Mapping[str, Any], sort: Any = None
    ) -> tuple[dict[str, Any], MatchContext] | None:
        found = self._matching(flt)
        if sort:
            docs = [doc for doc, _ in found]
            sort_documents(docs, sort)
            found = [next(item for item in found if item[0] is doc) for doc in docs]
        return found[0] if found else None

    # -- API --
    async def insert_one(self, document: dict[str, Any], session: Any = None) -> InsertOneResult:
        self._enter("insert_one")
        new = copy.deepcopy(document)
        new.setdefault("_id", ObjectId())
        self._store(new, None)
        return InsertOneResult(new["_id"], True)

    async def insert_many(
        self, documents: list[dict[str, Any]], ordered: bool = True, session: Any = None
    ) -> InsertManyResult:
        self._enter("insert_many")
        ids = []
        for position, document in enumerate(documents):
            new = copy.deepcopy(document)
            new.setdefault("_id", ObjectId())
            try:
                self._store(new, None)
            except DuplicateKeyError as exc:
                error = {"index": position, "code": 11000, **(exc.details or {})}
                raise BulkWriteError({"writeErrors": [error], "nInserted": position}) from None
            ids.append(new["_id"])
        return InsertManyResult(ids, True)

    async def find_one(
        self,
        filter: Mapping[str, Any] | None = None,
        projection: Mapping[str, Any] | None = None,
        sort: Any = None,
        session: Any = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        self._enter("find_one")
        found = self._first(filter or {}, sort)
        return None if found is None else project(found[0], projection)

    def find(
        self,
        filter: Mapping[str, Any] | None = None,
        projection: Mapping[str, Any] | None = None,
        sort: Any = None,
        limit: int = 0,
        hint: Any = None,
        session: Any = None,
    ) -> FakeCursor:
        self._enter("find")
        if hint is not None and hint not in self.indexes:
            raise OperationFailure("hint provided does not correspond to an existing index", 2)
        docs = [doc for doc, _ in self._matching(filter or {})]
        sort_documents(docs, sort)
        if limit:
            docs = docs[:limit]
        return FakeCursor([project(doc, projection) for doc in docs])

    async def count_documents(
        self, filter: Mapping[str, Any], session: Any = None, limit: int | None = None
    ) -> int:
        self._enter("count_documents")
        count = len(self._matching(filter))
        return min(count, limit) if limit else count

    async def update_one(
        self, filter: Mapping[str, Any], update: Any, upsert: bool = False, session: Any = None
    ) -> UpdateResult:
        self._enter("update_one")
        return self._update(filter, update, many=False, upsert=upsert)

    async def update_many(
        self, filter: Mapping[str, Any], update: Any, session: Any = None
    ) -> UpdateResult:
        self._enter("update_many")
        return self._update(filter, update, many=True, upsert=False)

    def _update(
        self, flt: Mapping[str, Any], update: Any, *, many: bool, upsert: bool
    ) -> UpdateResult:
        found = self._matching(flt)
        if not many:
            found = found[:1]
        if not found and upsert:
            base = {
                k: v for k, v in flt.items() if not k.startswith("$") and not _is_operator_dict(v)
            }
            new = apply_update(base, update, self._ctx(), inserting=True)
            new.setdefault("_id", ObjectId())
            self._store(new, None)
            return UpdateResult({"n": 1, "nModified": 0, "upserted": new["_id"]}, True)
        modified = 0
        for doc, ctx in found:
            new = apply_update(doc, update, ctx, inserting=False)
            if not equal(new, doc):
                self._store(new, doc)
                modified += 1
        return UpdateResult({"n": len(found), "nModified": modified}, True)

    async def find_one_and_update(
        self,
        filter: Mapping[str, Any],
        update: Any,
        projection: Mapping[str, Any] | None = None,
        sort: Any = None,
        upsert: bool = False,
        return_document: bool = ReturnDocument.BEFORE,
        session: Any = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        self._enter("find_one_and_update")
        found = self._first(filter, sort)
        if found is None:
            return None
        doc, ctx = found
        new = apply_update(doc, update, ctx, inserting=False)
        self._store(new, doc)
        return project(new if return_document == ReturnDocument.AFTER else doc, projection)

    async def replace_one(
        self, filter: Mapping[str, Any], replacement: dict[str, Any], session: Any = None
    ) -> UpdateResult:
        self._enter("replace_one")
        found = self._first(filter)
        if found is None:
            return UpdateResult({"n": 0, "nModified": 0}, True)
        doc = found[0]
        new = {"_id": doc["_id"], **copy.deepcopy(replacement)}
        self._store(new, doc)
        return UpdateResult({"n": 1, "nModified": 1}, True)

    async def delete_one(self, filter: Mapping[str, Any], session: Any = None) -> DeleteResult:
        self._enter("delete_one")
        found = self._first(filter)
        if found is None:
            return DeleteResult({"n": 0}, True)
        self.docs.remove(found[0])
        return DeleteResult({"n": 1}, True)

    async def delete_many(self, filter: Mapping[str, Any], session: Any = None) -> DeleteResult:
        self._enter("delete_many")
        doomed = [doc for doc, _ in self._matching(filter)]
        self.docs = [doc for doc in self.docs if all(doc is not d for d in doomed)]
        return DeleteResult({"n": len(doomed)}, True)

    async def create_indexes(self, models: list[Any], session: Any = None) -> list[str]:
        self._enter("create_indexes")
        names = []
        for model in models:
            spec = model.document
            definition = IndexDef(
                name=spec["name"],
                keys=list(spec["key"].items()),
                unique=bool(spec.get("unique")),
                partial=spec.get("partialFilterExpression"),
            )
            self.indexes[definition.name] = definition
            names.append(definition.name)
        return names

    async def index_information(self) -> dict[str, Any]:
        self._enter("index_information")
        info: dict[str, Any] = {"_id_": {"key": [("_id", 1)], "v": 2}}
        for index in self.indexes.values():
            item: dict[str, Any] = {"key": list(index.keys), "v": 2}
            if index.unique:
                item["unique"] = True
            if index.partial is not None:
                item["partialFilterExpression"] = copy.deepcopy(index.partial)
            info[index.name] = item
        return info


def _nullable(value: Any) -> Any:
    return None if value is MISSING else value


def _walk_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


# --------------------------------------------------------------- database --
class FakeDatabase:
    def __init__(self, name: str = "voice_agent_rnd") -> None:
        self.name = name
        self.collections: dict[str, FakeCollection] = {}
        self.now = datetime.now(UTC)
        self.operations = 0
        self._faults: dict[tuple[str, str], list[Fault]] = {}
        self.available = True

    def __getitem__(self, name: str) -> FakeCollection:
        return self.get_collection(name)

    def get_collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection(self, name)
        return self.collections[name]

    def fail(self, collection: str, method: str, factory: Fault, *, times: int = 1) -> None:
        """Make the next ``times`` calls of ``collection.method`` raise ``factory()``."""
        self._faults.setdefault((collection, method), []).extend([factory] * times)

    def raise_fault(self, collection: str, method: str) -> None:
        if not self.available:
            from pymongo.errors import ServerSelectionTimeoutError

            raise ServerSelectionTimeoutError("fake database unavailable")
        queue = self._faults.get((collection, method)) or self._faults.get(("*", method))
        if queue:
            raise queue.pop(0)()

    async def command(self, command: Any, **_: Any) -> dict[str, Any]:
        self.raise_fault("$cmd", "command")
        if command == "ping":
            return {"ok": 1}
        if isinstance(command, dict) and "collMod" in command:
            collection = self.get_collection(command["collMod"])
            collection.options = {k: v for k, v in command.items() if k != "collMod"}
            return {"ok": 1}
        raise NotImplementedError(f"fake command {command!r}")

    async def create_collection(self, name: str, **options: Any) -> FakeCollection:
        self.raise_fault("$cmd", "create_collection")
        if name in self.collections and self.collections[name].options.get("_created"):
            raise CollectionInvalid(f"collection {name} already exists")
        collection = self.get_collection(name)
        collection.options = {**options, "_created": True}
        return collection

    async def list_collections(self, filter: Mapping[str, Any] | None = None) -> FakeCursor:
        self.raise_fault("$cmd", "list_collections")
        rows = []
        for name, collection in self.collections.items():
            if not collection.options.get("_created"):
                continue
            row = {
                "name": name,
                "type": "collection",
                "options": {k: v for k, v in collection.options.items() if k != "_created"},
            }
            if filter is None or matches(row, filter, MatchContext(now=self.now)):
                rows.append(copy.deepcopy(row))
        return FakeCursor(rows)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {name: copy.deepcopy(c.docs) for name, c in self.collections.items()}

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        for name, docs in snapshot.items():
            self.collections[name].docs = docs


class FakeSession:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def with_transaction(self, callback: Callable[[Any], Any]) -> Any:
        snapshot = self._database.snapshot()
        try:
            return await callback(self)
        except BaseException:
            self._database.restore(snapshot)
            raise


class FakeClient:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.closed = False

    def get_database(self, name: str) -> FakeDatabase:
        assert name == self.database.name
        return self.database

    def start_session(self) -> FakeSession:
        return FakeSession(self.database)

    async def aclose(self) -> None:
        self.closed = True
