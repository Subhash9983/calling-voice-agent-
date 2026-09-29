"""Dispatch/job metadata locator: allowlisted fields and the 2 KiB bound (docs/06 §4)."""

from __future__ import annotations

import json

import pytest

from voice_agent.contracts.dispatch import (
    DISPATCH_METADATA_SCHEMA_VERSION,
    MAX_DISPATCH_METADATA_BYTES,
    DispatchLocator,
    DispatchMetadataError,
    decode_dispatch_metadata,
    encode_dispatch_metadata,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"
CONFIG_ID = "00000000-0000-4000-8000-000000000002"


def _locator() -> DispatchLocator:
    return DispatchLocator(
        session_id=SESSION_ID,
        correlation_id="corr-1",
        agent_config_id=CONFIG_ID,
        environment="development",
    )


def test_round_trip_is_compact_and_versioned() -> None:
    encoded = encode_dispatch_metadata(_locator())

    assert len(encoded.encode("utf-8")) <= MAX_DISPATCH_METADATA_BYTES
    assert json.loads(encoded)["schema_version"] == DISPATCH_METADATA_SCHEMA_VERSION
    assert decode_dispatch_metadata(encoded) == _locator()


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt": "hello"},
        {"token": "x"},
        {"transcript": "y"},
        {"agent_participant_id": "va-agent-1"},
    ],
)
def test_prohibited_or_unknown_fields_are_rejected(extra: dict[str, str]) -> None:
    raw = json.loads(encode_dispatch_metadata(_locator()))
    raw.update(extra)

    with pytest.raises(DispatchMetadataError) as caught:
        decode_dispatch_metadata(json.dumps(raw))

    assert "hello" not in str(caught.value)


def test_oversized_metadata_is_rejected_before_parsing() -> None:
    with pytest.raises(DispatchMetadataError):
        decode_dispatch_metadata("{" + " " * MAX_DISPATCH_METADATA_BYTES + "}")


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"schema_version": 2}'])
def test_malformed_metadata_is_rejected(raw: str) -> None:
    with pytest.raises(DispatchMetadataError):
        decode_dispatch_metadata(raw)


def test_environment_must_be_phase0() -> None:
    with pytest.raises(ValueError, match="environment"):
        DispatchLocator(
            session_id=SESSION_ID,
            correlation_id="c",
            agent_config_id=CONFIG_ID,
            environment="production",  # type: ignore[arg-type]
        )
