"""``python -m voice_agent.maintenance.evaluation`` over the in-process fake (no providers)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.evaluation_stores import evaluation_stores, persistence_for
from tests.support.fake_mongo import FakeDatabase

from voice_agent.domain.evaluation.common import EvaluationLayer
from voice_agent.evaluation.report import load_results
from voice_agent.evaluation.selection import RunSelection
from voice_agent.maintenance import evaluation as cli
from voice_agent.persistence.mongodb.bootstrap import apply_schema
from voice_agent.security.config_errors import ConfigDiagnostic, ConfigReason, ConfigurationError

SECRET = "wp12clisyntheticpass"  # noqa: S105 - synthetic canary, not a credential
ENV = {"MONGODB_URI": f"mongodb+srv://u:{SECRET}@cluster0.abcd1.mongodb.net/"}


def _run(
    capsys: pytest.CaptureFixture[str], argv: list[str], database: FakeDatabase | None = None
) -> tuple[int, dict[str, Any]]:
    db = database or FakeDatabase()
    code = cli.main(argv, environ=ENV, factory=lambda _env: persistence_for(db))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    body = json.loads(captured.out or captured.err)
    return code, body


def test_catalog_is_offline_and_passes_the_freeze_checklist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, body = _run(capsys, ["catalog"])

    assert code == 0
    assert body["freeze_checklist"] == {"passed": True, "problems": []}
    assert body["composition"]["total_case_count"] == 100
    assert body["composition"]["expected_result_slot_count"] == 240
    assert body["case_set_checksum"].startswith("sha256:")


def test_plan_shows_live_slots_pending_approval(capsys: pytest.CaptureFixture[str]) -> None:
    code, dev = _run(capsys, ["plan"])
    _, release = _run(capsys, ["plan", "--release-candidate", "phase0-rc1"])
    _, transcript = _run(capsys, ["plan", "--layers", "transcript"])

    assert code == 0
    assert dev["total_slots"] == 144 + 24 + 24
    assert dev["slots_by_layer"]["live_voice"] == {"pending_live_approval": 24}
    assert release["total_slots"] == 240
    assert release["splits"] == ["development", "holdout"]
    assert release["slots_by_layer"]["live_voice"] == {"pending_live_approval": 30}
    assert transcript["slots_by_layer"] == {"transcript_llm": {"executable": 144}}


def test_run_is_refused_pending_live_approval(capsys: pytest.CaptureFixture[str]) -> None:
    code, body = _run(capsys, ["run"])

    assert code == 0
    assert body["executed"] is False
    assert body["reason"] == "pending_live_approval"


def test_seed_dataset_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    database = FakeDatabase()
    asyncio.run(apply_schema(database))

    code, first = _run(capsys, ["seed-dataset"], database)
    _, second = _run(capsys, ["seed-dataset"], database)

    assert code == 0
    assert (first["outcome"], first["cases_added"], first["status"]) == ("created", 100, "frozen")
    assert second["outcome"] == "already_frozen"


def test_report_and_rate_on_a_stored_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async def prepare() -> tuple[FakeDatabase, str, str]:
        stores = await evaluation_stores()
        selection = RunSelection(layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-003",))
        outcome = await stores.runner().run(stores.request(selection))
        results = await load_results(stores.repositories.results, outcome.run.evaluation_run_id)
        return stores.database, outcome.run.evaluation_run_id, results[0].evaluation_result_id

    database, run_id, result_id = asyncio.run(prepare())
    output = tmp_path / "report.json"

    code, report = _run(capsys, ["report", "--run-id", run_id, "--output", str(output)], database)
    rate_code, rated = _run(
        capsys,
        [
            "rate",
            "--result-id",
            result_id,
            "--reviewer",
            "rd-reviewer-1",
            "--score",
            "correctness=4",
            "--score",
            "overall_conversation_quality=4",
        ],
        database,
    )

    assert code == 0
    assert report["release_verdict"]["phase0_baseline_passed"] is False
    assert json.loads(output.read_text(encoding="utf-8"))["command"] == "report"
    assert rate_code == 0
    assert rated["review_status"] == "complete"


def test_database_commands_fail_safely(capsys: pytest.CaptureFixture[str]) -> None:
    database = FakeDatabase()
    asyncio.run(apply_schema(database))
    missing = "00000000-0000-4000-8000-0000000000ff"

    assert _run(capsys, ["report"], database)[0] == 1
    code, body = _run(capsys, ["report", "--run-id", missing], database)
    assert (code, body["reason"]) == (1, "not_found")
    assert _run(capsys, ["rate", "--result-id", missing], database)[0] == 1
    with pytest.raises(SystemExit):
        cli.main(["rate", "--score", "bogus=9"], environ=ENV)


def test_report_reads_int_live_sessions_and_needs_twenty_valid_samples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def prepare() -> tuple[FakeDatabase, str]:
        stores = await evaluation_stores()
        selection = RunSelection(layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-003",))
        outcome = await stores.runner().run(stores.request(selection))
        return stores.database, outcome.run.evaluation_run_id

    database, run_id = asyncio.run(prepare())
    unknown = "00000000-0000-4000-8000-0000000000ee"

    code, report = _run(
        capsys, ["report", "--run-id", run_id, "--interruption-session-id", unknown], database
    )

    gates = {gate["gate_id"]: gate for gate in report["gates"]}
    assert code == 0
    assert report["pending_evidence"]["int_live"]["status"] == "collected"
    assert gates["interruption_p95_ms"]["status"] == "insufficient_samples"


def test_cli_failures_are_normalized_and_never_echo_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def unconfigured(_environ: Any) -> Any:
        missing = ConfigDiagnostic(ConfigReason.CREDENTIAL_MISSING, setting="MONGODB_URI")
        raise ConfigurationError((missing,))

    assert cli.main(["seed-dataset"], environ=ENV, factory=unconfigured) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "configuration_invalid"

    existing = tmp_path / "exists.json"
    existing.write_text("{}", encoding="utf-8")
    code, body = _run(capsys, ["catalog", "--output", str(existing)])
    assert (code, body["reason"]) == (1, "output_not_written")

    async def prepare() -> tuple[FakeDatabase, str]:
        stores = await evaluation_stores()
        selection = RunSelection(layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-003",))
        outcome = await stores.runner().run(stores.request(selection))
        results = await load_results(stores.repositories.results, outcome.run.evaluation_run_id)
        return stores.database, results[0].evaluation_result_id

    database, result_id = asyncio.run(prepare())
    argv = [
        "rate",
        "--result-id",
        result_id,
        "--reviewer",
        "not a safe ref!",
        "--score",
        "correctness=4",
    ]
    code, body = _run(capsys, argv, database)
    assert (code, body["reason"]) == (1, "rejected")
    assert "not a safe ref" not in json.dumps(body)
