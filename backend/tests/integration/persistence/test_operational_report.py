"""Bounded local operational report and retention evidence (docs/14 §17 WP11).

Read-only reconstruction of stored sessions into latency/cost/error/
coherence/finalization evidence, plus 30-day retention scheduling evidence.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    connecting,
    make_cost_run,
    make_error,
    make_event,
    make_operation,
    make_turn,
    write_context,
)

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventType
from voice_agent.domain.control_session import SessionRecord
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.maintenance.operational_report import operational_report, retention_report
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.errors import MongoErrorEventStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)

pytestmark = pytest.mark.asyncio
CONTEXT = EventWriteContext(environment="development", service_version="0.11.0")  # type: ignore[arg-type]


async def _stored_session(backend: Backend, *, terminal: bool = True) -> SessionRecord:
    record = connecting(await backend.session())
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.replace(record, expected_revision=0)
    clock = SystemClock()
    turn = make_turn(record.session_id, 1)
    await MongoTurnRepository(backend.persistence, context=write_context(record), clock=clock).save(
        turn.abandon()
    )
    operation = make_operation(record.session_id, turn.turn_id)
    await MongoOperationRepository(
        backend.persistence, context=write_context(record), clock=clock
    ).save(operation.cancel())
    await MongoErrorEventStore(backend.persistence).record(make_error(record))
    await MongoCostEntryStore(backend.persistence).insert_run(make_cost_run(record))
    log = MongoSessionEventLog(backend.persistence, context=CONTEXT)
    await log.append(make_event(record, EventType.SESSION_ACTIVE))
    if not terminal:
        return record
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=backend.now())
    await sessions.replace(failed, expected_revision=record.state_revision)
    await log.append(make_event(failed, EventType.SESSION_FAILED))
    return failed


async def test_single_session_report_reconstructs_safe_evidence(backend: Backend) -> None:
    record = await _stored_session(backend)

    report = await operational_report(
        backend.persistence,
        record.environment.value,
        now=backend.now(),
        session_id=record.session_id,
    )

    [session] = report["sessions"]
    assert session["session_id"] == record.session_id
    assert session["status"] == "failed"
    assert session["turns"]["abandoned"] == 1
    assert session["errors"]["total"] == 1
    assert session["finalization"]["terminal"] is True
    assert session["finalization"]["terminal_event_recorded"] is True
    assert session["finalization"]["open_operations"] == 0
    types = [entry["event_type"] for entry in session["timeline"]]
    assert types == ["session.active", "session.failed"]
    # The WP5 test cost run has no attempt-scope evidence and a test-only card:
    # the audit names the gap instead of inventing a total.
    assert session["cost"]["calculation_status"] == "final"
    assert session["coherent"] is False
    codes = {issue.split(":", 1)[0] for issue in session["issues"]}
    assert codes == {"rate_card_version_mismatch", "cost_not_reconciled"}
    assert session["cost"]["attempt_total_usd"] is None
    assert session["cost"]["unpriced_attempts"] == {"usage_unavailable": 1}
    assert report["aggregate"]["sessions"] == 1
    assert "final_transcript" not in json.dumps(report)


async def test_bounded_report_lists_newest_sessions_of_the_environment(backend: Backend) -> None:
    fake = backend.require_fake()
    del fake  # batch selection runs on the fake only (it reads the environment)
    first = await _stored_session(backend)
    second = await _stored_session(backend, terminal=False)

    report = await operational_report(
        backend.persistence, first.environment.value, now=backend.now(), limit=1
    )

    assert report["session_limit"] == 1
    assert [s["session_id"] for s in report["sessions"]] == [second.session_id]
    assert "timeline" not in report["sessions"][0]


async def test_unknown_session_reports_nothing(backend: Backend) -> None:
    report = await operational_report(
        backend.persistence,
        "development",
        now=backend.now(),
        session_id="00000000-0000-4000-8000-00000000beef",
    )

    assert report["sessions"] == []
    assert report["aggregate"]["sessions"] == 0
    assert report["aggregate"]["cost"]["total_usd"] is None


async def test_retention_report_shows_schedule_and_gaps(backend: Backend) -> None:
    fake = backend.require_fake()
    del fake  # environment-wide counts run on the fake only
    record = await _stored_session(backend)

    clean = await retention_report(
        backend.persistence, record.environment.value, now=backend.now(), sample=5
    )
    # Simulate a pre-WP11 child that was written after the terminal propagation.
    await backend.database[Collection.SESSION_EVENTS.value].update_many(
        {"session_id": record.session_id, "event_type": "session.failed"},
        {"$unset": {"expires_at": ""}},
    )
    gapped = await retention_report(
        backend.persistence,
        record.environment.value,
        now=backend.now() + timedelta(days=31),
        sample=5,
    )

    overview = clean["retention"]
    assert overview["policy_version"] == "rd_retention_30d_v1"
    assert overview["retention_days"] == 30
    assert overview["unscheduled_terminal_sessions"] == 0
    assert overview["complete"] is True
    assert overview["due_sessions"] == 0
    assert overview["next_due_at"] is not None
    later = gapped["retention"]
    assert later["due_sessions"] >= 1
    assert later["unscheduled_children"]["session_events"] == 1
    assert later["sessions_with_gaps"] == [record.session_id]
    assert later["complete"] is False
    assert Decimal(overview["terminal_sessions"]) >= 1
