"""Strict ``$jsonSchema`` validators derived from the record models (docs/02 §24).

Deriving the validator from the same strict Pydantic model that guards every
write keeps the two validation layers identical: required root fields, BSON
types, approved enums, string/array bounds, canonical UUID patterns, and
closed (``additionalProperties: false``) objects. Open allowlisted containers
(``dict[str, JsonValue]``) stay plain bounded objects whose keys the adapter
allowlists validate.
"""

from __future__ import annotations

import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from voice_agent.contracts import base as contract_base

UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
INTEGER_TYPES: Final = ["int", "long"]
NUMBER_TYPES: Final = ["double", "int", "long"]
Schema = dict[str, Any]


def model_schema(model: type[BaseModel], *, root: bool = False) -> Schema:
    """Closed object schema for ``model``; ``root`` adds the internal ``_id``."""
    properties: Schema = {}
    required: list[str] = []
    for name, info in model.model_fields.items():
        schema, nullable = _field_schema(info)
        properties[name] = schema
        if not nullable:
            required.append(name)
    if root:
        properties = {"_id": {"bsonType": "objectId"}, **properties}
    result: Schema = {"bsonType": "object"}
    if required:
        result["required"] = required
    result["properties"] = properties
    result["additionalProperties"] = False
    return result


def _field_schema(info: FieldInfo) -> tuple[Schema, bool]:
    return _annotation_schema(info.annotation, list(info.metadata))


def _flatten(metadata: list[Any]) -> list[Any]:
    flat: list[Any] = []
    for item in metadata:
        if isinstance(item, FieldInfo):
            flat.extend(_flatten(list(item.metadata)))
        else:
            flat.append(item)
    return flat


def _annotation_schema(annotation: Any, metadata: list[Any]) -> tuple[Schema, bool]:
    """Return ``(schema, nullable)`` for one annotation plus its constraints."""
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        inner, *extra = get_args(annotation)
        return _annotation_schema(inner, [*metadata, *extra])
    if origin in (Union, types.UnionType):
        return _union_schema(get_args(annotation), metadata)
    return _constrained(_base_schema(annotation), _flatten(metadata)), False


def _union_schema(args: tuple[Any, ...], metadata: list[Any]) -> tuple[Schema, bool]:
    members = [arg for arg in args if arg is not type(None)]
    nullable = len(members) != len(args)
    schemas = [_annotation_schema(member, [])[0] for member in members]
    if len(schemas) == 1:
        return _constrained(schemas[0], _flatten(metadata)), nullable
    return {"anyOf": schemas}, nullable


def _literal_schema(values: tuple[Any, ...]) -> Schema:
    kinds = {_bson_type_of(value) for value in values}
    kind: Any = kinds.pop() if len(kinds) == 1 else sorted(kinds)
    return {"bsonType": kind, "enum": list(values)}


def _bson_type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    return "string"


def _sequence_schema(annotation: Any, *, unique: bool) -> Schema:
    args = [arg for arg in get_args(annotation) if arg is not Ellipsis]
    schema: Schema = {"bsonType": "array"}
    if args:
        schema["items"] = _annotation_schema(args[0], [])[0]
    if unique:
        schema["uniqueItems"] = True
    return schema


def _mapping_schema(annotation: Any) -> Schema:
    args = get_args(annotation)
    value = args[1] if len(args) == 2 else Any
    item = _annotation_schema(value, [])[0]
    return {"bsonType": "object", "additionalProperties": item} if item else {"bsonType": "object"}


def _base_schema(annotation: Any) -> Schema:
    origin = get_origin(annotation)
    if annotation is Any or type(annotation).__name__ == "TypeAliasType":
        return {}  # JsonValue/any: shape bounded by the model validator
    if origin is Literal:
        return _literal_schema(get_args(annotation))
    if origin in (tuple, list):
        return _sequence_schema(annotation, unique=False)
    if origin in (set, frozenset):
        return _sequence_schema(annotation, unique=True)
    if origin is dict:
        return _mapping_schema(annotation)
    if isinstance(annotation, type):
        return _type_schema(annotation)
    raise TypeError(f"unsupported annotation for a validator: {annotation!r}")


def _type_schema(annotation: type) -> Schema:
    if issubclass(annotation, BaseModel):
        return model_schema(annotation)
    if issubclass(annotation, Enum):
        return {"bsonType": "string", "enum": [member.value for member in annotation]}
    if issubclass(annotation, bool):
        return {"bsonType": "bool"}
    if issubclass(annotation, int):
        return {"bsonType": list(INTEGER_TYPES)}
    if issubclass(annotation, float):
        return {"bsonType": list(NUMBER_TYPES)}
    if issubclass(annotation, Decimal):
        return {"bsonType": "decimal"}
    if issubclass(annotation, datetime | date):
        return {"bsonType": "date"}
    if issubclass(annotation, str):
        return {"bsonType": "string"}
    if annotation is dict:
        return {"bsonType": "object"}
    raise TypeError(f"unsupported type for a validator: {annotation!r}")


def _constrained(schema: Schema, metadata: list[Any]) -> Schema:
    result = dict(schema)
    kind = result.get("bsonType")
    for item in metadata:
        _apply_constraint(result, kind, item)
    return result


def _apply_constraint(result: Schema, kind: Any, item: Any) -> None:
    """Translate one Pydantic/annotated-types constraint (matched by shape)."""
    name = type(item).__name__
    is_array = kind == "array"
    if name == "MinLen":
        result["minItems" if is_array else "minLength"] = item.min_length
    elif name == "MaxLen":
        result["maxItems" if is_array else "maxLength"] = item.max_length
    elif name in _BOUNDS:
        key, exclusive = _BOUNDS[name]
        result[key] = _number(getattr(item, name.lower()))
        if exclusive:
            result["exclusiveMinimum" if key == "minimum" else "exclusiveMaximum"] = True
    elif getattr(item, "pattern", None) is not None:
        result["pattern"] = str(item.pattern)
    elif _validator_function(item) is contract_base._require_canonical_uuid:
        result.update({"minLength": 36, "maxLength": 36, "pattern": UUID_PATTERN})
    elif _validator_function(item) is contract_base._require_non_negative:
        result["minimum"] = 0


_BOUNDS: Final = {
    "Ge": ("minimum", False),
    "Gt": ("minimum", True),
    "Le": ("maximum", False),
    "Lt": ("maximum", True),
}


def _validator_function(item: Any) -> Any:
    return getattr(item, "func", None)


def _number(value: Any) -> int | float:
    return value if isinstance(value, int) and not isinstance(value, bool) else float(value)
