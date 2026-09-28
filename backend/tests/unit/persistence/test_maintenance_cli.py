"""``python -m voice_agent.maintenance.database`` against the in-process fake."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.support.fake_mongo import FakeClient, FakeDatabase

from voice_agent.maintenance import database as cli
from voice_agent.persistence.mongodb.client import MongoPersistence

SECRET = "wp5clisyntheticpass"  # noqa: S105 - synthetic canary, not a credential
ENV = {"MONGODB_URI": f"mongodb+srv://u:{SECRET}@cluster0.abcd1.mongodb.net/"}


def _factory(database: FakeDatabase) -> Any:
    return lambda _environ: MongoPersistence.from_handles(FakeClient(database), database)


def _run(capsys: pytest.CaptureFixture[str], argv: list[str], database: FakeDatabase) -> Any:
    code = cli.main(argv, environ=ENV, factory=_factory(database))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    return code, captured


def test_apply_verify_seed_and_cleanup(capsys: pytest.CaptureFixture[str]) -> None:
    database = FakeDatabase()

    code_verify_before, before = _run(capsys, ["verify"], database)
    code_apply, applied = _run(capsys, ["apply-schema"], database)
    code_seed, seeded = _run(capsys, ["seed-configs"], database)
    code_again, again = _run(capsys, ["seed-configs"], database)
    code_clean, cleaned = _run(capsys, ["cleanup"], database)

    assert code_verify_before == 0
    assert json.loads(before.out)["conforms"] is False
    assert code_apply == 0
    assert json.loads(applied.out)["conforms"] is True
    assert len(json.loads(applied.out)["created_collections"]) == 14
    assert (code_seed, code_again, code_clean) == (0, 0, 0)
    assert json.loads(seeded.out)["seeded"][0]["outcome"] == "inserted"
    assert json.loads(again.out)["seeded"][0]["outcome"] == "present"
    report = json.loads(cleaned.out)
    assert report["sessions"]["dry_run"] is True
    assert report["evaluation"]["dry_run"] is True


def test_unavailable_database_fails_safely(capsys: pytest.CaptureFixture[str]) -> None:
    database = FakeDatabase()
    database.available = False

    code, captured = _run(capsys, ["verify"], database)

    assert code == 1
    assert json.loads(captured.err) == {"ok": False, "reason": "database_unavailable"}


def test_missing_uri_is_a_configuration_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["verify"], environ={})
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err) == {"ok": False, "reason": "configuration_invalid"}
