from __future__ import annotations

from pathlib import Path

import pytest

from decision_ledger.event_store import resolve_ledger_paths


def test_resolve_ledger_paths_uses_nearest_decision_ledger_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECISION_LEDGER_HOME", raising=False)
    monkeypatch.delenv("DECISION_LEDGER_DB", raising=False)
    root_home = tmp_path / ".decision-ledger"
    nested = tmp_path / "repo" / "src"
    root_home.mkdir()
    nested.mkdir(parents=True)

    paths = resolve_ledger_paths(cwd=nested)

    assert paths.home == root_home.resolve()
    assert paths.db_path == root_home.resolve() / "ledger.sqlite"
    assert paths.events_dir == root_home.resolve() / "events"


def test_resolve_ledger_paths_uses_db_parent_as_home_for_explicit_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECISION_LEDGER_HOME", raising=False)
    monkeypatch.delenv("DECISION_LEDGER_DB", raising=False)
    db_path = tmp_path / "custom.sqlite"

    paths = resolve_ledger_paths(db_path=db_path, cwd=tmp_path)

    assert paths.home == tmp_path.resolve()
    assert paths.db_path == db_path.resolve()
    assert paths.events_dir == tmp_path.resolve() / "events"
