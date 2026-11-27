from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from decision_ledger import event_store as event_store_module
from decision_ledger.db import connect
from decision_ledger.event_store import EventedLedger, EventStore

SUBJECT = "decision-ledger.safety.ordering"


def make_ledger(home: Path) -> EventedLedger:
    db_path = home / "ledger.sqlite"
    return EventedLedger(connect(db_path), EventStore(home), db_path)


def add(ledger: EventedLedger, body: str = "body") -> str:
    return ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body=body)


def event_file(home: Path) -> Path:
    return EventStore(home).event_path(SUBJECT)


def record_count(ledger: EventedLedger) -> int:
    return int(ledger.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def test_projection_failure_writes_no_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = make_ledger(tmp_path)
    add(ledger, "first")
    before = event_file(tmp_path).read_bytes()
    calls = {"n": 0}
    real_apply = event_store_module.apply_event

    def failing_once(conn: sqlite3.Connection, event: dict[str, object]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated projection failure")
        real_apply(conn, event)

    monkeypatch.setattr(event_store_module, "apply_event", failing_once)

    with pytest.raises(sqlite3.OperationalError):
        add(ledger, "second")
    assert event_file(tmp_path).read_bytes() == before
    assert record_count(ledger) == 1

    add(ledger, "second")
    lines = event_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert record_count(ledger) == 2


def test_event_write_failure_rolls_back_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = make_ledger(tmp_path)

    def failing_write(self: EventStore, event: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(EventStore, "write", failing_write)

    with pytest.raises(OSError):
        add(ledger)
    assert record_count(ledger) == 0
    assert not event_file(tmp_path).exists()


def test_commit_failure_after_write_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = make_ledger(tmp_path)
    monkeypatch.setattr(event_store_module, "COMMIT_RETRY_DELAY_SECONDS", 0)

    def failing_commit(self: EventedLedger) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(EventedLedger, "_commit", failing_commit)

    record_id = add(ledger)

    assert record_id.startswith("rec_")
    assert len(event_file(tmp_path).read_text(encoding="utf-8").splitlines()) == 1
    assert ledger.write_warnings and "rebuild" in ledger.write_warnings[0]
    assert (tmp_path / event_store_module.STALE_MARKER).exists()

    monkeypatch.undo()
    ledger.conn.rollback()
    ledger.rebuild()
    assert ledger.get_record(record_id) is not None
    assert not (tmp_path / event_store_module.STALE_MARKER).exists()


def test_rebuild_skips_and_reports_an_event_that_cannot_apply(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    good = add(ledger, "good")
    bad = {
        "event_id": "evt_poison",
        "event_type": "associated",
        "subject": SUBJECT,
        "created_at": "2026-11-26T20:00:00+00:00",
        "created_by": "test",
        "note": None,
        "record_id": "rec_missing",
        "payload": {"association_id": "asc_x", "from_record_id": "rec_missing", "to_record_id": "rec_missing_too", "relation": "supports", "note": None},
    }
    with event_file(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(bad) + "\n")
    after = add(ledger, "after")

    ledger.rebuild()

    assert ledger.get_record(good) is not None
    assert ledger.get_record(after) is not None
    assert [item["event_id"] for item in ledger.last_rebuild_skipped] == ["evt_poison"]


def _append_many(home: str, worker: int, count: int, size: int) -> None:
    store = EventStore(home)
    for index in range(count):
        store.append(
            subject=SUBJECT,
            event_type="created",
            record_id=f"rec_{worker}_{index}",
            payload={"body": str(worker) * size},
        )


def test_concurrent_large_appends_do_not_interleave(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_append_many, args=(str(tmp_path), worker, 100, 20_000)) for worker in range(3)]
    for process in workers:
        process.start()
    for process in workers:
        process.join(60)
        assert process.exitcode == 0

    lines = event_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 300
    for line in lines:
        json.loads(line)
