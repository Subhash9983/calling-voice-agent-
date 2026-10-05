"""Explicit, scoped R&D database administration (docs/03 §23; docs/02 §20, §24).

``python -m voice_agent.maintenance.database <command>`` where command is:

- ``verify``        read-only schema conformance report;
- ``apply-schema``  additively create missing collections/validators/indexes;
- ``seed-configs``  insert the built-in non-secret configurations if absent;
- ``cleanup``       bounded expired-session and evaluation cleanup; dry-run
  unless ``--apply`` is given;
- ``report``        read-only bounded operational report (latency, cost,
  errors, coherence, finalization) of the newest ``--limit`` sessions, or
  one ``--session-id`` with its timeline (WP11);
- ``retention-report`` read-only 30-day retention evidence (WP11).

``--output PATH`` additionally writes the report to a new local evidence
file (e.g. under ``outputs/``); an existing file is never overwritten.

The MongoDB URI is loaded only through the WP3 loader and never printed.
Only the approved database is opened; nothing is ever dropped. Output is a
safe JSON report of names, counts, IDs, and normalized codes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.maintenance.operational_report import (
    DEFAULT_REPORT_SESSIONS,
    operational_report,
    retention_report,
    write_report_file,
)
from voice_agent.persistence.mongodb.bootstrap import apply_schema, verify_schema
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.repositories.evaluation_cleanup import (
    MongoEvaluationCleanupStore,
)
from voice_agent.persistence.mongodb.repositories.retention import MongoRetentionCleanupStore
from voice_agent.persistence.mongodb.seed import seed_agent_configs
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError
from voice_agent.privacy_and_retention.cleanup import RetentionCleanupJob
from voice_agent.privacy_and_retention.expiry import MAX_CLEANUP_BATCH_SESSIONS
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.security.config_errors import (
    ConfigDiagnostic,
    ConfigReason,
    ConfigurationError,
)
from voice_agent.security.config_loader import load_bootstrap_configuration

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
COMMANDS: Final = (
    "verify",
    "apply-schema",
    "seed-configs",
    "cleanup",
    "report",
    "retention-report",
)
PersistenceFactory = Callable[[Mapping[str, str] | None], MongoPersistence]
Handler = Callable[[MongoPersistence, argparse.Namespace, str], Awaitable[dict[str, Any]]]


def _default_factory(environ: Mapping[str, str] | None) -> MongoPersistence:
    settings = load_bootstrap_configuration(environ).settings
    if settings.mongodb_uri is None:
        missing = ConfigDiagnostic(ConfigReason.CREDENTIAL_MISSING, setting="MONGODB_URI")
        raise ConfigurationError((missing,))
    return MongoPersistence(settings.mongodb_uri, database_name=settings.mongodb_database)


async def _verify(
    persistence: MongoPersistence, _args: argparse.Namespace, _env: str
) -> dict[str, Any]:
    return (await verify_schema(persistence.database)).to_safe_dict()


async def _apply(
    persistence: MongoPersistence, _args: argparse.Namespace, _env: str
) -> dict[str, Any]:
    return (await apply_schema(persistence.database)).to_safe_dict()


async def _seed(
    persistence: MongoPersistence, _args: argparse.Namespace, _env: str
) -> dict[str, Any]:
    results = await seed_agent_configs(persistence, builtin_agent_config_documents())
    return {
        "seeded": [
            {"agent_config_id": r.agent_config_id, "outcome": r.outcome.value} for r in results
        ]
    }


async def _cleanup(
    persistence: MongoPersistence, args: argparse.Namespace, environment: str
) -> dict[str, Any]:
    clock = SystemClock()
    dry_run = not args.apply
    sessions = await RetentionCleanupJob(MongoRetentionCleanupStore(persistence), clock=clock).run(
        environment, dry_run=dry_run, limit=args.limit
    )
    evaluation = await MongoEvaluationCleanupStore(persistence).run(
        environment, now=clock.utc_now(), limit=args.limit, dry_run=dry_run
    )
    return {
        "sessions": sessions.to_safe_dict(),
        "evaluation": {
            "dry_run": evaluation.dry_run,
            "abandoned_runs": evaluation.abandoned_runs,
            "expiry_marked_runs": evaluation.expiry_marked_runs,
            "candidates": evaluation.candidates,
            "deleted": evaluation.deleted,
        },
    }


async def _report(
    persistence: MongoPersistence, args: argparse.Namespace, environment: str
) -> dict[str, Any]:
    return await operational_report(
        persistence,
        environment,
        now=SystemClock().utc_now(),
        limit=args.limit,
        session_id=args.session_id,
    )


async def _retention(
    persistence: MongoPersistence, args: argparse.Namespace, environment: str
) -> dict[str, Any]:
    return await retention_report(
        persistence, environment, now=SystemClock().utc_now(), sample=args.limit
    )


HANDLERS: Final[Mapping[str, Handler]] = {
    "verify": _verify,
    "apply-schema": _apply,
    "seed-configs": _seed,
    "cleanup": _cleanup,
    "report": _report,
    "retention-report": _retention,
}


def _session_id(value: str) -> str:
    """Canonical lowercase UUID only (never echoed back on error)."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("session id must be a canonical UUID") from None
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("session id must be a canonical UUID")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scoped R&D database administration.")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--apply", action="store_true", help="cleanup: delete (default dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="batch/session bound")
    parser.add_argument("--session-id", type=_session_id, default=None, help="report: one session")
    parser.add_argument("--output", type=Path, default=None, help="also write a new local file")
    return parser


def _with_default_limit(args: argparse.Namespace) -> argparse.Namespace:
    if args.limit is None:
        reporting = args.command in {"report", "retention-report"}
        default = DEFAULT_REPORT_SESSIONS if reporting else MAX_CLEANUP_BATCH_SESSIONS
        return argparse.Namespace(**{**vars(args), "limit": default})
    return args


async def _run(
    persistence: MongoPersistence, args: argparse.Namespace, environment: str
) -> dict[str, Any]:
    persistence.open()
    try:
        return await HANDLERS[args.command](persistence, args, environment)
    finally:
        await persistence.close()


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    factory: PersistenceFactory = _default_factory,
) -> int:
    args = _with_default_limit(_parser().parse_args(argv))
    try:
        environment = load_bootstrap_configuration(environ).settings.app_env.value
        persistence = factory(environ)
    except ConfigurationError:
        sys.stderr.write(json.dumps({"ok": False, "reason": "configuration_invalid"}) + "\n")
        return EXIT_FAILED
    try:
        report = asyncio.run(_run(persistence, args, environment))
    except (StoreUnavailableError, PersistenceRejectedError):
        sys.stderr.write(json.dumps({"ok": False, "reason": "database_unavailable"}) + "\n")
        return EXIT_FAILED
    payload = {"ok": True, "command": args.command, **report}
    if args.output is not None:
        try:
            write_report_file(args.output, payload)
        except OSError:
            sys.stderr.write(json.dumps({"ok": False, "reason": "output_not_written"}) + "\n")
            return EXIT_FAILED
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
