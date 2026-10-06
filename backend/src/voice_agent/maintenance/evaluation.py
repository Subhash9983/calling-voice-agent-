"""Phase 0 evaluation maintenance CLI (docs/14 §18, docs/17).

``python -m voice_agent.maintenance.evaluation <command>``:

- ``catalog``       offline: build the docs/17 dataset, run the §23 freeze
  checklist, print composition and the case-set checksum (no database);
- ``plan``          offline: the slot plan for a selection (3/1/3
  repetitions); live voice slots show ``pending_live_approval``;
- ``seed-dataset``  create and freeze the dataset through the WP5
  repositories (idempotent);
- ``report``        the baseline report of one stored run (``--run-id``), with
  ``INT-LIVE`` samples from ``--interruption-session-id`` sessions;
- ``rate``          capture one reviewer's human rating for a result and
  refresh its review summary;
- ``run``           refused: live provider execution needs separate approval;
  offline runs execute through the pytest harness.

No command calls a speech/LLM provider. Output is safe JSON; the MongoDB URI
is loaded only through the WP3 loader and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from voice_agent.domain.evaluation.common import (
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
    RatingDimension,
)
from voice_agent.domain.evaluation.dataset import case_set_checksum
from voice_agent.domain.evaluation.rating import EvaluationHumanRating, RatingScores, RatingStatus
from voice_agent.evaluation.catalog import CatalogContext, build_catalog, freeze_checklist
from voice_agent.evaluation.codes import RUBRIC_VERSION
from voice_agent.evaluation.live_evidence import interruption_evidence
from voice_agent.evaluation.report import run_report
from voice_agent.evaluation.seeding import seed_catalog
from voice_agent.evaluation.selection import RunSelection, plan_slots, select_cases
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.events_and_latency.evidence_loader import load_session_evidence
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.maintenance.database import (
    EXIT_FAILED,
    EXIT_OK,
    PersistenceFactory,
    default_persistence_factory,
)
from voice_agent.maintenance.operational_report import evidence_sources, write_report_file
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.repositories.evaluation_definitions import (
    MongoEvaluationCaseRepository,
    MongoEvaluationDatasetRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_results import (
    MongoEvaluationRatingRepository,
    MongoEvaluationResultRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_runs import (
    MongoEvaluationRunRepository,
)
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError, ReferenceNotFoundError
from voice_agent.security.config_errors import ConfigurationError
from voice_agent.security.config_loader import load_bootstrap_configuration

OFFLINE_COMMANDS: Final = ("catalog", "plan", "run")
DATABASE_COMMANDS: Final = ("seed-dataset", "report", "rate")
SEED_ACTOR: Final = "wp12-evaluation-seed"
_LAYER_ALIASES: Final = {
    "transcript": EvaluationLayer.TRANSCRIPT_LLM,
    "live": EvaluationLayer.LIVE_VOICE,
    "reliability": EvaluationLayer.RELIABILITY_FAILURE,
}
Handler = Callable[[MongoPersistence, argparse.Namespace, str], Any]


def _canonical_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("identifier must be a canonical UUID") from None
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("identifier must be a canonical UUID")
    return value


def _score(value: str) -> tuple[str, int]:
    name, _, raw = value.partition("=")
    if name not in {d.value for d in RatingDimension} or raw not in {"1", "2", "3", "4", "5"}:
        raise argparse.ArgumentTypeError("score must be <approved_dimension>=<1..5>")
    return name, int(raw)


def _context(environment: str, now: datetime, source_revision: str) -> CatalogContext:
    return CatalogContext(EvaluationEnvironment(environment), SEED_ACTOR, now, source_revision)


def _selection(args: argparse.Namespace) -> tuple[RunSelection, EvaluationPurpose]:
    layers = tuple(_LAYER_ALIASES[name] for name in args.layers) or tuple(EvaluationLayer)
    splits = [EvaluationSplit.DEVELOPMENT]
    if args.release_candidate:
        splits.append(EvaluationSplit.HOLDOUT)
    selection = RunSelection(
        layers=layers,
        splits=tuple(splits),
        case_keys=tuple(args.case_keys),
        release_candidate=args.release_candidate,
        unseal_holdout=bool(args.release_candidate),
    )
    purpose = EvaluationPurpose.RELEASE if args.release_candidate else EvaluationPurpose.DEVELOPMENT
    return selection, purpose


# ------------------------------------------------------------- offline --
def _catalog(environment: str, now: datetime, args: argparse.Namespace) -> dict[str, Any]:
    dataset, cases = build_catalog(_context(environment, now, args.source_revision))
    problems = freeze_checklist(cases)
    checksum = case_set_checksum([(c.sequence_number, c.case_key, c.case_checksum) for c in cases])
    return {
        "dataset_key": dataset.dataset_key,
        "version": dataset.version,
        "composition": dataset.composition.model_dump(mode="json"),
        "case_set_checksum": checksum,
        "freeze_checklist": {"passed": not problems, "problems": list(problems)},
    }


def _plan(environment: str, now: datetime, args: argparse.Namespace) -> dict[str, Any]:
    _, cases = build_catalog(_context(environment, now, args.source_revision))
    selection, purpose = _selection(args)
    slots = plan_slots(select_cases(cases, selection, purpose), live_approved=False)
    by_layer: dict[str, dict[str, int]] = {}
    for slot in slots:
        layer = by_layer.setdefault(slot.layer.value, {})
        layer[slot.disposition.value] = layer.get(slot.disposition.value, 0) + 1
    return {
        "purpose": purpose.value,
        "splits": [split.value for split in selection.splits],
        "total_slots": len(slots),
        "slots_by_layer": by_layer,
    }


def _run(_environment: str, _now: datetime, _args: argparse.Namespace) -> dict[str, Any]:
    return {
        "executed": False,
        "reason": "pending_live_approval",
        "detail": (
            "Live provider evaluation (transcript LLM, live voice, INT-LIVE) needs a separately "
            "approved budget (docs/14 §18). Offline fixture/reliability runs execute through "
            "the pytest harness."
        ),
    }


OFFLINE: Final[Mapping[str, Callable[[str, datetime, argparse.Namespace], dict[str, Any]]]] = {
    "catalog": _catalog,
    "plan": _plan,
    "run": _run,
}


# ------------------------------------------------------------ database --
async def _seed(persistence: MongoPersistence, args: argparse.Namespace, env: str) -> Any:
    result = await seed_catalog(
        MongoEvaluationDatasetRepository(persistence),
        MongoEvaluationCaseRepository(persistence),
        _context(env, SystemClock().utc_now(), args.source_revision),
    )
    return {
        "evaluation_dataset_id": result.dataset.evaluation_dataset_id,
        "status": result.dataset.status.value,
        "outcome": result.outcome.value,
        "cases_added": result.cases_added,
        "case_set_checksum": result.dataset.case_set_checksum,
    }


async def _report(persistence: MongoPersistence, args: argparse.Namespace, _env: str) -> Any:
    interruption = None
    if args.interruption_session_id:
        sources = evidence_sources(persistence)

        async def load(session_id: str) -> SessionEvidenceInput | None:
            return await load_session_evidence(sources, session_id)

        interruption = await interruption_evidence(args.interruption_session_id, load)
    report = await run_report(
        MongoEvaluationRunRepository(persistence),
        MongoEvaluationResultRepository(persistence),
        args.run_id,
        now=SystemClock().utc_now(),
        interruption=interruption,
    )
    if report is None:
        raise ReferenceNotFoundError("evaluation run not found")
    return report


async def _rate(persistence: MongoPersistence, args: argparse.Namespace, env: str) -> Any:
    results = MongoEvaluationResultRepository(persistence)
    ratings = MongoEvaluationRatingRepository(persistence)
    result = await results.get(args.result_id)
    if result is None:
        raise ReferenceNotFoundError("evaluation result not found")
    now = SystemClock().utc_now()
    rating = EvaluationHumanRating(
        evaluation_human_rating_id=str(uuid.uuid4()),
        evaluation_result_id=result.evaluation_result_id,
        evaluation_run_id=result.evaluation_run_id,
        evaluation_dataset_id=result.evaluation_dataset_id,
        evaluation_case_id=result.evaluation_case_id,
        reviewer_ref=args.reviewer,
        rubric_version=RUBRIC_VERSION,
        rating_revision=1,
        status=RatingStatus.DRAFT,
        is_current=True,
        scores=RatingScores(**dict(args.score)),
        reason_codes=tuple(args.reason),
        comment=args.comment,
        environment=EvaluationEnvironment(env),
        created_at=now,
        updated_at=now,
    )
    await ratings.create_draft(rating)
    submitted = await ratings.submit(rating.evaluation_human_rating_id, now=now)
    summary = await results.recompute_review_summary(
        result.evaluation_result_id,
        expected_revision=result.result_revision,
        expected_reviewers=max(result.human_review_summary.expected_reviewer_count, 1),
        now=now,
    )
    return {
        "evaluation_human_rating_id": submitted.evaluation_human_rating_id,
        "review_status": summary.human_review_summary.review_status,
    }


DATABASE: Final[Mapping[str, Handler]] = {
    "seed-dataset": _seed,
    "report": _report,
    "rate": _rate,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 evaluation maintenance (no providers).")
    parser.add_argument("command", choices=(*OFFLINE_COMMANDS, *DATABASE_COMMANDS))
    parser.add_argument("--source-revision", default="docs17-v1")
    parser.add_argument("--layers", nargs="*", choices=sorted(_LAYER_ALIASES), default=[])
    parser.add_argument("--case-keys", nargs="*", default=[])
    parser.add_argument("--release-candidate", default=None, help="unseal holdout for this RC")
    parser.add_argument("--run-id", type=_canonical_id, default=None)
    parser.add_argument("--result-id", type=_canonical_id, default=None)
    parser.add_argument(
        "--interruption-session-id",
        type=_canonical_id,
        action="append",
        default=[],
        help="report: INT-LIVE / live barge-in session (repeatable)",
    )
    parser.add_argument("--reviewer", default=None)
    parser.add_argument("--score", type=_score, action="append", default=[])
    parser.add_argument("--reason", action="append", default=[])
    parser.add_argument("--comment", default=None)
    parser.add_argument("--output", type=Path, default=None, help="also write a new local file")
    return parser


def _missing_arguments(args: argparse.Namespace) -> str | None:
    if args.command == "report" and args.run_id is None:
        return "report requires --run-id"
    if args.command == "rate" and (args.result_id is None or not args.reviewer or not args.score):
        return "rate requires --result-id, --reviewer, and at least one --score"
    return None


async def _database(
    persistence: MongoPersistence, args: argparse.Namespace, environment: str
) -> Any:
    persistence.open()
    try:
        return await DATABASE[args.command](persistence, args, environment)
    finally:
        await persistence.close()


def _emit(payload: dict[str, Any], output: Path | None) -> int:
    if output is not None:
        try:
            write_report_file(output, payload)
        except OSError:
            sys.stderr.write(json.dumps({"ok": False, "reason": "output_not_written"}) + "\n")
            return EXIT_FAILED
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return EXIT_OK


def _fail(reason: str) -> int:
    sys.stderr.write(json.dumps({"ok": False, "reason": reason}) + "\n")
    return EXIT_FAILED


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    factory: PersistenceFactory = default_persistence_factory,
) -> int:
    args = _parser().parse_args(argv)
    missing = _missing_arguments(args)
    if missing is not None:
        return _fail(missing.replace(" ", "_")[:60])
    try:
        environment = load_bootstrap_configuration(environ).settings.app_env.value
        if args.command in OFFLINE:
            body = OFFLINE[args.command](environment, SystemClock().utc_now(), args)
            return _emit({"ok": True, "command": args.command, **body}, args.output)
        persistence = factory(environ)
    except ConfigurationError:
        return _fail("configuration_invalid")
    try:
        body = asyncio.run(_database(persistence, args, environment))
    except ReferenceNotFoundError:
        return _fail("not_found")
    except (StoreUnavailableError, PersistenceRejectedError):
        return _fail("database_unavailable")
    except ValueError:  # domain rule or validation rejection (never echoes input)
        return _fail("rejected")
    return _emit({"ok": True, "command": args.command, **body}, args.output)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
