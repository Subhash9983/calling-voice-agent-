"""Explicit-dispatch job metadata locator (docs/06 §4; docs/05 §3).

Dispatch metadata is an untrusted locator, never authoritative configuration.
It carries only the allowlisted fields below and is bounded to 2 KiB encoded;
the worker reloads and validates the durable session and exact configuration
before any paid provider starts. Errors never echo the rejected input.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import ValidationError

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, StrictModel

DISPATCH_METADATA_SCHEMA_VERSION: Final = 1
MAX_DISPATCH_METADATA_BYTES: Final = 2 * 1024


class DispatchMetadataError(ValueError):
    """The job metadata is missing, malformed, oversized, or not allowlisted."""


class DispatchLocator(StrictModel):
    schema_version: Literal[1] = DISPATCH_METADATA_SCHEMA_VERSION
    session_id: CanonicalId
    correlation_id: ExternalIdentifier
    agent_config_id: CanonicalId
    environment: Literal["development", "rd"]


def encode_dispatch_metadata(locator: DispatchLocator) -> str:
    encoded = locator.model_dump_json()
    if len(encoded.encode("utf-8")) > MAX_DISPATCH_METADATA_BYTES:  # pragma: no cover - bounded
        raise DispatchMetadataError("dispatch metadata exceeds 2 KiB")
    return encoded


def decode_dispatch_metadata(raw: str) -> DispatchLocator:
    if len(raw.encode("utf-8")) > MAX_DISPATCH_METADATA_BYTES:
        raise DispatchMetadataError("dispatch metadata exceeds 2 KiB")
    try:
        return DispatchLocator.model_validate_json(raw)
    except ValidationError:
        pass
    raise DispatchMetadataError("dispatch metadata is not a valid locator")
