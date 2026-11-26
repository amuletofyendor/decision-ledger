"""Pin every test to its own temporary ledger home so no test can reach a real ledger.

Events are canonical and append-only: a test event written into ``~/Dev/.decision-ledger`` (or
any home named by ``DECISION_LEDGER_HOME``) could not be removed cleanly. Every test therefore
runs with the ledger variables unset, ``HOME`` and the working directory inside its own
temporary directory, and fails at once if it opens an event store or a SQLite file anywhere
else.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decision_ledger import db as ledger_db
from decision_ledger import event_store

_EVENT_STORE_INIT = event_store.EventStore.__init__
_SQLITE_CONNECT = sqlite3.connect


@pytest.fixture(autouse=True)
def pinned_ledger_home(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Unset the ledger variables and refuse any ledger path outside pytest's temporary root."""
    root = tmp_path_factory.getbasetemp().resolve()

    def require_inside(path: str | os.PathLike[str]) -> None:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_relative_to(root):
            pytest.fail(
                f"test used ledger path {resolved} outside the test's temporary directory {root}",
                pytrace=False,
            )

    def guarded_init(self: event_store.EventStore, home: str | Path) -> None:
        require_inside(home)
        _EVENT_STORE_INIT(self, home)

    def guarded_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if isinstance(database, (str, os.PathLike)):
            text = os.fspath(database)
            if text and text != ":memory:" and not text.startswith("file:"):
                require_inside(text)
        connection: sqlite3.Connection = _SQLITE_CONNECT(database, *args, **kwargs)
        return connection

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("DECISION_LEDGER_HOME", raising=False)
    monkeypatch.delenv("DECISION_LEDGER_DB", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ledger_db, "DEFAULT_DB_PATH", home / ".decision-ledger" / "ledger.sqlite"
    )
    monkeypatch.setattr(event_store, "DEFAULT_LEDGER_HOME", home / ".decision-ledger")
    monkeypatch.setattr(event_store.EventStore, "__init__", guarded_init)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield home
