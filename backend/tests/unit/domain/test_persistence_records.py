"""Domain invariants of the new durable records (docs/02 §10-§13; docs/16 §5-§9)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.support.persistence_builders import (
    make_calculation,
    make_case,
    make_config,
    make_consent,
    make_cost_run,
    make_dataset,
    make_error,
    make_rating,
    make_result,
    make_run,
    make_session,
    now_ms,
    rate_card,
    run_context,
)

from voice_agent.contracts.cost import Currency, FxRate, FxType
from voice_agent.contracts.failures import ErrorType
from voice_agent.costing.cost_entries import cost_entry_for_line
from voice_agent.domain.consent import (
    ConsentDecision,
    ConsentFulfilment,
    FulfilmentStatus,
)
from voice_agent.domain.cost_entry import CostScope
from voice_agent.domain.error_event import ErrorSeverity, ResolutionStatus
from voice_agent.domain.errors import DomainRuleError, InvalidTransitionError
from voice_agent.domain.evaluation.common import EvaluationPurpose
from voice_agent.domain.evaluation.dataset import DatasetStatus, composition_of
from voice_agent.domain.evaluation.result import InvalidationSource, ResultStatus, Validity
from voice_agent.domain.evaluation.run import RunStatus
from voice_agent.domain.records_common import bounded_container, unique_items
from voice_agent.domain.worker_lease import LeaseToken


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    return make_session(make_config())


def test_error_event_classification_and_resolution(session) -> None:  # type: ignore[no-untyped-def]
    error = make_error(session)
    expected = make_error(session, error_type=ErrorType.CANCELLATION)

    assert expected.is_expected
    assert not expected.counts_toward_failure_rate
    assert expected.severity is ErrorSeverity.INFO
    assert expected.resolution.status is ResolutionStatus.IGNORED_EXPECTED
    retrying = error.resolve(status=ResolutionStatus.RETRYING, action=None, now=now_ms())
    assert retrying.resolved_at is None
    assert retrying.resolution.revision == 1
    with pytest.raises(DomainRuleError):
        error.resolve(status=ResolutionStatus.OPEN, action=None, now=now_ms())
    with pytest.raises(ValidationError):
        error.model_validate({**error.model_dump(), "category": "quota"})
    with pytest.raises(ValidationError):
        error.model_validate({**error.model_dump(), "is_expected": True})


def test_consent_rules(session) -> None:  # type: ignore[no-untyped-def]
    grant = make_consent(session)

    assert grant.verify_checksum()
    with pytest.raises(ValidationError):
        make_consent(session, decision=ConsentDecision.REVOKED)  # no superseded grant
    with pytest.raises(DomainRuleError):
        grant.with_fulfilment(ConsentFulfilment(status=FulfilmentStatus.PENDING, revision=5))
    with pytest.raises(DomainRuleError):
        grant.with_fulfilment(
            ConsentFulfilment(status=FulfilmentStatus.PENDING, revision=1)
        ).with_evidence_expiry(grant.decision_at + timedelta(days=30))
    with pytest.raises(ValidationError):
        grant.with_evidence_expiry(grant.decision_at + timedelta(days=1))
    with pytest.raises(ValidationError):
        grant.model_validate({**grant.model_dump(), "data_categories": ["user_audio"] * 2})


def test_cost_entry_scope_target_and_fx_evidence(session) -> None:  # type: ignore[no-untyped-def]
    (entry,) = make_cost_run(session)

    assert entry.scope_target_id == session.session_id
    with pytest.raises(ValidationError):
        entry.model_validate({**entry.model_dump(), "scope": CostScope.TURN})
    with pytest.raises(ValidationError):
        entry.model_validate({**entry.model_dump(), "scope": CostScope.OPERATION})
    turn = entry.model_validate(
        {**entry.model_dump(), "scope": CostScope.TURN, "turn_id": session.session_id}
    )
    assert turn.scope_target_id == session.session_id
    (line,) = make_calculation().lines
    inr = line.model_copy(update={"original_currency": Currency.INR})
    card = rate_card().model_copy(
        update={
            "fx_rates": (
                FxRate(
                    source_currency=Currency.INR,
                    target_currency=Currency.USD,
                    rate=Decimal("0.012"),
                    fx_type=FxType.PLANNING,
                    source_reference="planning-fx",
                    effective_date=now_ms().date(),
                ),
            )
        }
    )
    with_fx = cost_entry_for_line(
        inr, card=card, context=run_context(session), cost_entry_id=session.session_id
    )
    without = cost_entry_for_line(
        inr, card=rate_card(), context=run_context(session), cost_entry_id=session.session_id
    )
    estimated = cost_entry_for_line(
        line.model_copy(update={"estimated": True}),
        card=rate_card(),
        context=run_context(session),
        cost_entry_id=session.session_id,
    )
    assert with_fx.currency_conversion.fx_source == "planning-fx"
    assert without.currency_conversion.fx_source == "rate_card_fx"
    # An estimated non-token quantity is not labelled as tokens (WP11 label fix).
    assert estimated.calculation_method == "estimated_quantity_x_public_rate"


def test_dataset_lifecycle_rules() -> None:
    dataset = make_dataset()
    case = make_case(dataset, 1)
    composition = composition_of([(case.layer, case.split, "en", "medium")])

    frozen = dataset.freeze(
        composition=composition, checksum=case.case_checksum, actor="t", now=now_ms()
    )
    assert frozen.status is DatasetStatus.FROZEN
    assert frozen.revision == 1
    with pytest.raises(DomainRuleError):
        frozen.freeze(composition=composition, checksum=case.case_checksum, actor="t", now=now_ms())
    with pytest.raises(DomainRuleError):
        frozen.edit_draft(now=now_ms(), name="x")
    with pytest.raises(DomainRuleError):
        dataset.edit_draft(now=now_ms(), status="frozen")
    with pytest.raises(DomainRuleError):
        dataset.retire(actor="t", now=now_ms())
    release = make_dataset(purpose=EvaluationPurpose.RELEASE)
    with pytest.raises(DomainRuleError):
        release.freeze(
            composition=composition, checksum=case.case_checksum, actor="t", now=now_ms()
        )
    with pytest.raises(ValidationError):
        dataset.model_validate({**dataset.model_dump(), "version": 2})
    with pytest.raises(ValidationError):
        dataset.model_validate({**dataset.model_dump(), "tags": ["a", "a"]})


def test_case_rules() -> None:
    case = make_case(make_dataset(), 1)

    assert case.verify_checksum()
    assert not case.model_copy(update={"category": "other"}).verify_checksum()
    with pytest.raises(ValidationError):
        case.model_validate({**case.model_dump(), "layer": "live_voice"})
    with pytest.raises(ValidationError):
        case.model_validate({**case.model_dump(), "repetition_policy": {"repetitions": 1}})
    duplicated = [case.automated_assertions[0].model_dump()] * 2
    with pytest.raises(ValidationError):
        case.model_validate({**case.model_dump(), "automated_assertions": duplicated})


def test_run_transitions_and_expiry() -> None:
    dataset = make_dataset()
    case = make_case(dataset, 1)
    frozen = dataset.freeze(
        composition=composition_of([(case.layer, case.split, "en", "medium")]),
        checksum=case.case_checksum,
        actor="t",
        now=now_ms(),
    )
    run = make_run(frozen, make_config())

    with pytest.raises(InvalidTransitionError):
        run.transition(RunStatus.COMPLETED, now=now_ms())
    with pytest.raises(DomainRuleError):
        run.with_expiry()
    ended = run.transition(RunStatus.FAILED, now=now_ms())
    assert ended.with_expiry().expires_at == ended.ended_at + timedelta(days=30)  # type: ignore[operator]
    with pytest.raises(DomainRuleError):
        ended.with_progress(run.progress, now=now_ms())
    summarized = run.with_summary({"quality": 1}, now=now_ms())
    assert summarized.status_revision == 1
    with pytest.raises(ValidationError):
        run.model_validate({**run.model_dump(), "status": "running"})  # no started_at


def test_result_and_rating_rules() -> None:
    dataset = make_dataset()
    case = make_case(dataset, 1)
    frozen = dataset.freeze(
        composition=composition_of([(case.layer, case.split, "en", "medium")]),
        checksum=case.case_checksum,
        actor="t",
        now=now_ms(),
    )
    result = make_result(make_run(frozen, make_config()), case)

    with pytest.raises(DomainRuleError):
        result.finalize(status=ResultStatus.INVALID, now=now_ms())
    started = result.start(now=now_ms())
    with pytest.raises(DomainRuleError):
        started.start(now=now_ms())
    with pytest.raises(DomainRuleError):
        started.finalize(status=ResultStatus.COMPLETED, now=now_ms(), validity=None)
    done = started.finalize(status=ResultStatus.COMPLETED, now=now_ms())
    with pytest.raises(DomainRuleError):
        done.finalize(status=ResultStatus.COMPLETED, now=now_ms())
    with pytest.raises(DomainRuleError):
        done.mark_invalid(Validity(is_valid_sample=True), now=now_ms())
    with pytest.raises(ValidationError):
        Validity(is_valid_sample=False)
    invalid = done.mark_invalid(
        Validity(
            is_valid_sample=False,
            reason_code="harness",
            invalidation_source=InvalidationSource.HARNESS,
        ),
        now=now_ms(),
    )
    assert invalid.superseded(now=now_ms()).is_current_attempt is False
    rating = make_rating(result)
    with pytest.raises(DomainRuleError):
        rating.superseded(now=now_ms())
    with pytest.raises(DomainRuleError):
        rating.model_copy(update={"scores": None}).submit(now=now_ms())
    no_overall = rating.model_copy(
        update={
            "scores": rating.scores.model_copy(  # type: ignore[union-attr]
                update={"overall_conversation_quality": None}
            )
        }
    )
    with pytest.raises(DomainRuleError):
        no_overall.submit(now=now_ms())
    submitted = rating.submit(now=now_ms())
    with pytest.raises(DomainRuleError):
        submitted.submit(now=now_ms())
    with pytest.raises(ValidationError):
        rating.model_validate({**rating.model_dump(), "rating_revision": 2})


def test_common_helpers_and_lease_deadline() -> None:
    check = bounded_container(20, 1).func
    with pytest.raises(ValueError, match="keys"):
        check({"a": 1, "b": 2})
    with pytest.raises(ValueError, match="bytes"):
        check({"a": "x" * 50})
    with pytest.raises(ValueError, match="unique"):
        unique_items(("a", "a"))
    now = now_ms()
    token = LeaseToken(
        session_id="00000000-0000-4000-8000-000000000001",
        generation=1,
        worker_instance_id="w",
        livekit_job_id="j",
        writer_epoch=1,
        lease_revision=1,
        lease_expires_at=now + timedelta(seconds=15),
    )
    assert token.local_deadline(now) == now + timedelta(seconds=12)
    assert token.renewed(lease_expires_at=now).lease_revision == 2
