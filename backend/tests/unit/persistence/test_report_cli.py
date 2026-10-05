"""``report`` / ``retention-report`` maintenance commands against the in-process fake (WP11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.fake_mongo import FakeClient, FakeDatabase

from voice_agent.maintenance import database as cli
from voice_agent.persistence.mongodb.client import MongoPersistence

SECRET = "wp11clisyntheticpass"  # noqa: S105 - synthetic canary, not a credential
ENV = {"MONGODB_URI": f"mongodb+srv://u:{SECRET}@cluster0.abcd1.mongodb.net/"}


def _factory(database: FakeDatabase) -> Any:
    return lambda _environ: MongoPersistence.from_handles(FakeClient(database), database)


def _run(capsys: pytest.CaptureFixture[str], argv: list[str], database: FakeDatabase) -> Any:
    code = cli.main(argv, environ=ENV, factory=_factory(database))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    return code, captured


def _ready(capsys: pytest.CaptureFixture[str]) -> FakeDatabase:
    database = FakeDatabase()
    assert _run(capsys, ["apply-schema"], database)[0] == 0
    return database


def test_report_on_an_empty_environment_is_bounded_and_safe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _ready(capsys)

    code, captured = _run(capsys, ["report"], database)

    assert code == 0
    report = json.loads(captured.out)
    assert report["command"] == "report"
    assert report["session_limit"] == 20
    assert report["sessions"] == []
    assert report["aggregate"]["sessions"] == 0


def test_report_limit_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    database = _ready(capsys)

    code, captured = _run(capsys, ["report", "--limit", "5000"], database)

    assert code == 0
    assert json.loads(captured.out)["session_limit"] == 100


def test_retention_report_is_read_only(capsys: pytest.CaptureFixture[str]) -> None:
    database = _ready(capsys)

    code, captured = _run(capsys, ["retention-report"], database)

    assert code == 0
    retention = json.loads(captured.out)["retention"]
    assert retention["terminal_sessions"] == 0
    assert retention["complete"] is True


def test_output_writes_a_new_local_evidence_file_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = _ready(capsys)
    target = tmp_path / "outputs" / "wp11" / "report.json"

    first, _ = _run(capsys, ["report", "--output", str(target)], database)
    second, refused = _run(capsys, ["report", "--output", str(target)], database)

    assert first == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["command"] == "report"
    assert second == 1
    assert json.loads(refused.err) == {"ok": False, "reason": "output_not_written"}
    assert json.loads(target.read_text(encoding="utf-8")) == written  # never overwritten


@pytest.mark.parametrize(
    "value", ["not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "$where:1"]
)
def test_session_id_must_be_canonical_and_is_not_echoed(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["report", "--session-id", value], environ=ENV, factory=_factory(FakeDatabase()))
    captured = capsys.readouterr()

    assert exited.value.code == 2
    assert value not in captured.out + captured.err
