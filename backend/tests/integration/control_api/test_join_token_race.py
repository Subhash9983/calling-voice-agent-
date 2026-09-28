"""WP4 carry-over: concurrent join-token refreshes never lose audit entries (docs/02 §6)."""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.control_api.conftest import API, ApiFactory, new_id

pytestmark = pytest.mark.asyncio


async def test_concurrent_join_token_refreshes_keep_every_entry(api_factory: ApiFactory) -> None:
    async with api_factory() as api:
        session_id = await api.new_session_id()
        ids = [new_id() for _ in range(5)]
        responses = await asyncio.gather(
            *(
                api.client.post(
                    f"{API}/sessions/{session_id}/join-token",
                    json={"client_request_id": request_id},
                )
                for request_id in ids
            )
        )
        record = await api.sessions.get(session_id)

    assert {response.status_code for response in responses} == {200}
    assert record is not None
    assert {entry.client_request_id for entry in record.join_token_requests} == set(ids)


async def test_end_after_join_does_not_erase_join_evidence(api_factory: ApiFactory) -> None:
    async with api_factory() as api:
        session_id = await api.new_session_id()
        join, end = await asyncio.gather(
            api.client.post(
                f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
            ),
            api.client.post(
                f"{API}/sessions/{session_id}/end",
                json={"client_request_id": new_id(), "reason": "user_ended"},
            ),
        )
        record = await api.sessions.get(session_id)

    assert end.status_code == 202
    assert record is not None
    if join.status_code == 200:
        assert len(record.join_token_requests) == 1
