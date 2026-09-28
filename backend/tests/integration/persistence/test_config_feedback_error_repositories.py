"""``agent_configs``, ``user_feedback``, and ``error_events`` contracts (docs/02 §5, §11, §12)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    make_config,
    make_error,
    make_failure,
    make_feedback,
    make_turn,
    new_id,
    write_context,
)

from voice_agent.domain.agent_config import AgentConfigStatus
from voice_agent.domain.error_event import ResolutionAction, ResolutionStatus
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.feedback import ResolutionCode, ReviewStatus
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.errors import (
    MongoErrorEventRepository,
    MongoErrorEventStore,
)
from voice_agent.persistence.mongodb.repositories.feedback import MongoFeedbackRepository
from voice_agent.persistence.mongodb.repositories.timeline import MongoTurnRepository
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.persistence import PersistenceRejectedError, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- configs --
async def test_config_round_trip_and_active_listing(backend: Backend) -> None:
    config = await backend.config()
    configs = MongoAgentConfigRepository(backend.persistence)

    assert await configs.get(config.agent_config_id) == config
    assert await configs.get_active(config.agent_config_id) == config
    assert config.agent_config_id in {
        c.agent_config_id for c in await configs.list_active("development")
    }
    assert [c.version for c in await configs.list_versions(config.agent_id, limit=5)] == [1]


async def test_config_checksum_and_version_uniqueness_are_enforced(backend: Backend) -> None:
    config = await backend.config()
    configs = MongoAgentConfigRepository(backend.persistence)
    tampered = make_config().model_copy(update={"name": "silently edited"})
    backend.tracker.configs.add(tampered.agent_config_id)
    same_version = make_config(agent_id=config.agent_id)
    backend.tracker.configs.add(same_version.agent_config_id)

    with pytest.raises(PersistenceRejectedError):
        await configs.insert(tampered)
    with pytest.raises(DuplicateKeyError):
        await configs.insert(same_version)


async def test_one_active_version_per_agent_and_lifecycle_revisions(backend: Backend) -> None:
    active = await backend.config()
    configs = MongoAgentConfigRepository(backend.persistence)
    draft = await backend.config(
        agent_id=active.agent_id, version=2, status="draft", change_note="Synthetic second version."
    )

    with pytest.raises(DuplicateKeyError):
        await configs.activate(
            draft.agent_config_id,
            expected_revision=draft.revision,
            actor="wp5-test",
            now=backend.now(),
        )
    retired = await configs.retire(
        active.agent_config_id,
        expected_revision=active.revision,
        actor="wp5-test",
        now=backend.now(),
    )
    activated = await configs.activate(
        draft.agent_config_id, expected_revision=draft.revision, actor="wp5-test", now=backend.now()
    )

    assert retired.status is AgentConfigStatus.RETIRED
    assert retired.revision == active.revision + 1
    assert activated.status is AgentConfigStatus.ACTIVE
    assert activated.verify_checksum()
    with pytest.raises(RevisionConflictError):
        await configs.retire(
            active.agent_config_id,
            expected_revision=active.revision,
            actor="wp5-test",
            now=backend.now(),
        )


async def test_retired_config_expiry_respects_references(backend: Backend) -> None:
    config = await backend.config()
    configs = MongoAgentConfigRepository(backend.persistence)
    await backend.session(config)  # live (nonterminal) referencing session
    retired = await configs.retire(
        config.agent_config_id,
        expected_revision=config.revision,
        actor="wp5-test",
        now=backend.now(),
    )
    assert retired.retired_at is not None

    with pytest.raises(DomainRuleError):
        await configs.mark_expiry(
            config.agent_config_id,
            expected_revision=retired.revision,
            expires_at=retired.retired_at + timedelta(days=31),
            now=backend.now(),
        )


async def test_retired_unreferenced_config_can_be_marked_for_expiry(backend: Backend) -> None:
    config = await backend.config()
    configs = MongoAgentConfigRepository(backend.persistence)
    retired = await configs.retire(
        config.agent_config_id,
        expected_revision=config.revision,
        actor="wp5-test",
        now=backend.now(),
    )
    assert retired.retired_at is not None

    with pytest.raises(DomainRuleError):
        await configs.mark_expiry(
            config.agent_config_id,
            expected_revision=retired.revision,
            expires_at=retired.retired_at + timedelta(days=10),
            now=backend.now(),
        )
    marked = await configs.mark_expiry(
        config.agent_config_id,
        expected_revision=retired.revision,
        expires_at=retired.retired_at + timedelta(days=30),
        now=backend.now(),
    )
    assert marked.expires_at == retired.retired_at + timedelta(days=30)


# -------------------------------------------------------------- feedback --
async def test_feedback_insert_idempotency_and_round_trip(backend: Backend) -> None:
    record = await backend.session()
    feedback = MongoFeedbackRepository(backend.persistence)
    item = make_feedback(record)

    await feedback.insert(item)
    with pytest.raises(DuplicateKeyError):
        await feedback.insert(item.model_copy(update={"feedback_id": new_id()}))

    assert await feedback.get_by_client_submission_id(item.client_submission_id) == item
    assert [f.feedback_id for f in await feedback.list_for_session(record.session_id, limit=5)] == [
        item.feedback_id
    ]


async def test_feedback_targets_must_belong_to_the_session(backend: Backend) -> None:
    record = await backend.session()
    other = await backend.session()
    turn = make_turn(other.session_id, 1)
    await MongoTurnRepository(
        backend.persistence, context=write_context(other), clock=SystemClock()
    ).save(turn)

    with pytest.raises(ReferenceNotFoundError):
        await MongoFeedbackRepository(backend.persistence).insert(
            make_feedback(record, turn_id=turn.turn_id)
        )


async def test_feedback_review_is_the_only_mutable_section(backend: Backend) -> None:
    record = await backend.session()
    feedback = MongoFeedbackRepository(backend.persistence)
    item = make_feedback(record)
    await feedback.insert(item)

    reviewed = await feedback.record_review(
        item.feedback_id,
        expected_review_revision=0,
        status=ReviewStatus.RESOLVED,
        reviewer="rd-reviewer-1",
        now=backend.now(),
        resolution_code=ResolutionCode.FIXED,
    )
    queue = await feedback.review_queue("development", ReviewStatus.RESOLVED, limit=100)

    assert reviewed.review.review_revision == 1
    assert reviewed.content == item.content
    assert item.feedback_id in {f.feedback_id for f in queue}
    with pytest.raises(RevisionConflictError):
        await feedback.record_review(
            item.feedback_id,
            expected_review_revision=0,
            status=ReviewStatus.DISMISSED,
            reviewer="rd-reviewer-1",
            now=backend.now(),
        )


# ---------------------------------------------------------------- errors --
async def test_error_delivery_is_deduplicated_and_queryable(backend: Backend) -> None:
    record = await backend.session()
    errors = MongoErrorEventStore(backend.persistence)
    error = make_error(record)

    assert await errors.record(error) is True
    assert await errors.record(error) is False
    second = make_error(record)
    await errors.record(second)

    assert await errors.get(error.error_id) == error
    assert len(await errors.list_for_session(record.session_id, limit=10)) == 2
    assert len(await errors.list_by_fingerprint(error.error_fingerprint, limit=10)) >= 2
    queue = await errors.resolution_queue("development", ResolutionStatus.OPEN, limit=100)
    assert error.error_id in {e.error_id for e in queue}


async def test_error_resolution_is_revision_checked(backend: Backend) -> None:
    record = await backend.session()
    errors = MongoErrorEventStore(backend.persistence)
    error = make_error(record)
    await errors.record(error)

    resolved = await errors.resolve(
        error.error_id,
        expected_revision=0,
        status=ResolutionStatus.RECOVERED,
        action=ResolutionAction.RETRY_SUCCEEDED,
        now=backend.now(),
    )

    assert resolved.resolution.revision == 1
    assert resolved.resolved_at is not None
    assert resolved.error_type is error.error_type
    with pytest.raises(RevisionConflictError):
        await errors.resolve(
            error.error_id,
            expected_revision=0,
            status=ResolutionStatus.UNRECOVERABLE,
            action=None,
            now=backend.now(),
        )


async def test_error_requires_existing_session_and_worker_adapter_maps(backend: Backend) -> None:
    record = await backend.session()
    orphan = make_error(record).model_copy(update={"session_id": new_id()})
    adapter = MongoErrorEventRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    error_id = new_id()

    with pytest.raises(ReferenceNotFoundError):
        await MongoErrorEventStore(backend.persistence).record(orphan)
    await adapter.add(error_id, make_failure(record.session_id))
    stored = await MongoErrorEventStore(backend.persistence).get(error_id)

    assert stored is not None
    assert stored.correlation_id == record.correlation_id
    assert stored.counts_toward_failure_rate is True
