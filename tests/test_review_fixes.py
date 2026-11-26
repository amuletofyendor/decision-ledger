"""Second-review fixes for fix/ledger-safety (night-reviews-20261126/09-review-ledger-safety.md)."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from decision_ledger import db as ledger_db
from decision_ledger import vector_search
from decision_ledger.db import connect
from decision_ledger.event_store import EventedLedger, EventStore, resolve_ledger_paths
from decision_ledger.guidance import SERVER_INSTRUCTIONS
from decision_ledger.repository import Ledger
from decision_ledger.vector_search import METADATA_TABLE

SUBJECT = "decision-ledger.safety.review"


class FakeEmbedder:
    provider = "fake"
    model = "fake-2d"
    dimensions = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] if "vector" in text.lower() else [1.0, 0.0] for text in texts]


def make_ledger(tmp_path: Path) -> EventedLedger:
    db_path = tmp_path / "ledger.sqlite"
    return EventedLedger(connect(db_path), EventStore(tmp_path), db_path)


# F-1: the suite can never reach a real ledger home


def test_ledger_environment_is_cleared_for_every_test(tmp_path: Path) -> None:
    assert "DECISION_LEDGER_HOME" not in os.environ
    assert "DECISION_LEDGER_DB" not in os.environ
    assert Path.home().resolve().is_relative_to(tmp_path.parent.parent)
    paths = resolve_ledger_paths(db_path=tmp_path / "ledger.sqlite")
    assert paths.home == tmp_path.resolve()


def test_an_event_store_outside_the_test_directory_fails_the_test(tmp_path: Path) -> None:
    outside = Path("/tmp") / "decision-ledger-guard-probe-never-created"
    with pytest.raises(pytest.fail.Exception, match="outside the test's temporary directory"):
        EventStore(outside)
    assert not outside.exists()


def test_a_projection_outside_the_test_directory_fails_the_test() -> None:
    outside = Path("/tmp") / "decision-ledger-guard-probe.sqlite"
    with pytest.raises(pytest.fail.Exception, match="outside the test's temporary directory"):
        connect(outside)
    assert not outside.exists()


# F-3: agents are told not to retry a write that came back with warnings


def test_guidance_says_a_write_with_warnings_must_not_be_retried() -> None:
    text = " ".join(SERVER_INSTRUCTIONS.split())
    assert "Do not retry a write that returned warnings" in text


# F-4: the first switch to WAL does not stop the server starting


def test_connect_continues_in_the_old_journal_mode_when_wal_cannot_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ledger.sqlite"
    old = sqlite3.connect(path, check_same_thread=False)
    old.execute("PRAGMA journal_mode = DELETE")
    old.execute("CREATE TABLE t (x)")
    old.commit()
    old.execute("BEGIN")
    old.execute("SELECT * FROM t").fetchall()
    monkeypatch.setattr(ledger_db, "WAL_SWITCH_TIMEOUT_MS", 100)
    # The old reader finishes shortly after the WAL attempt gives up, as a short MCP read would.
    release = threading.Timer(0.5, old.rollback)
    release.start()
    try:
        conn = connect(path)
    finally:
        release.join()
        old.close()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert "WAL" in capsys.readouterr().err
    conn.close()
    assert connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# F-5: an empty or partial vector index is reported, not silent


def test_vector_search_reports_an_empty_index_while_records_exist(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body="vector body")
    ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body="other body")
    repo = Ledger(ledger.conn)
    full = repo.vector_search("vector", embedder=FakeEmbedder(), fail_soft=False)
    assert full["available"] is True and "warning" not in full
    ledger.conn.execute(f"DELETE FROM {METADATA_TABLE}")
    ledger.conn.commit()
    empty = vector_search.vector_search_records(ledger.conn, "vector", embedder=FakeEmbedder(), build_missing=False)
    assert empty["available"] is False
    assert empty["coverage"] == {"embedded": 0, "records": 2}
    assert "empty" in empty["warning"]


def test_hybrid_search_carries_a_partial_index_warning(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body="vector body")
    ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body="other body")
    repo = Ledger(ledger.conn)
    repo.vector_search("vector", embedder=FakeEmbedder(), fail_soft=False)
    one = ledger.conn.execute(f"SELECT record_id FROM {METADATA_TABLE} LIMIT 1").fetchone()[0]
    ledger.conn.execute(f"DELETE FROM {METADATA_TABLE} WHERE record_id = ?", (one,))
    ledger.conn.commit()
    partial = vector_search.vector_search_records(
        ledger.conn, "vector", embedder=FakeEmbedder(), build_missing=False
    )
    assert partial["available"] is True
    assert partial["coverage"] == {"embedded": 1, "records": 2}
    assert "1 of 2" in partial["warning"]


# F-6: a new event file's directory entry is made durable


def test_creating_an_event_file_fsyncs_its_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path)
    synced: list[str] = []
    real = os.fsync

    def record(fd: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real(fd)

    monkeypatch.setattr(os, "fsync", record)
    event = store.build(
        event_type="record_added", subject="decision-ledger.safety.fsync", record_id="rec_x", payload={"id": "x"}
    )
    store.write(event)
    path = store.event_path("decision-ledger.safety.fsync")
    assert str(path) in synced and str(path.parent) in synced
    synced.clear()
    store.write(event)
    assert synced == [str(path)]
