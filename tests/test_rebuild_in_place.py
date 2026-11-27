from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from decision_ledger.cli import main
from decision_ledger.db import SCHEMA_PATH, connect
from decision_ledger.event_store import PROJECTION_TABLES, EventedLedger, EventStore

SUBJECT = "decision-ledger.safety.rebuild"


def make_ledger(home: Path) -> EventedLedger:
    db_path = home / "ledger.sqlite"
    return EventedLedger(connect(db_path), EventStore(home), db_path)


def add(ledger: EventedLedger, body: str) -> str:
    return ledger.add_record(subject=SUBJECT, kind="thought", status="active", summary=None, body=body)


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def test_rebuild_keeps_the_same_file_and_other_connections_see_it(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    writer = make_ledger(tmp_path)
    add(writer, "one")
    other = make_ledger(tmp_path)
    inode_before = os.stat(db_path).st_ino

    writer.rebuild()

    assert os.stat(db_path).st_ino == inode_before
    assert count(other.conn) == 1
    late = add(other, "written by the other connection after the rebuild")
    fresh = connect(db_path)
    assert fresh.execute("SELECT 1 FROM records WHERE id = ?", (late,)).fetchone() is not None
    assert count(fresh) == 2


def test_rebuild_replaces_rows_that_the_events_do_not_support(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    kept = add(ledger, "kept")
    ledger.conn.execute(
        "INSERT INTO records(id, subject, kind, status, body, created_at) VALUES ('rec_stray', 's.t', 'thought', 'active', 'x', '2026-01-01')"
    )
    ledger.conn.commit()

    ledger.rebuild()

    ids = [row[0] for row in ledger.conn.execute("SELECT id FROM records")]
    assert ids == [kept]
    assert [row[0] for row in ledger.conn.execute("SELECT rowid FROM records_fts WHERE records_fts MATCH 'kept'")]


def test_rebuild_proceeds_while_a_reader_is_open(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    add(ledger, "one")
    reader = connect(tmp_path / "ledger.sqlite")
    reader.execute("BEGIN")
    assert count(reader) == 1
    add(ledger, "two")

    ledger.rebuild()

    reader.rollback()
    assert count(reader) == 2


def test_projection_table_list_covers_the_schema() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
    assert tables == set(PROJECTION_TABLES)


def test_cli_recreate_requires_force(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"
    assert main(["--db", str(db_path), "add", SUBJECT, "--kind", "thought", "--body", "x"]) == 0
    inode_before = os.stat(db_path).st_ino

    assert main(["--db", str(db_path), "rebuild", "--recreate", "--skip-vectors"]) == 2
    assert "--force" in capsys.readouterr().err
    assert os.stat(db_path).st_ino == inode_before

    assert main(["--db", str(db_path), "rebuild", "--recreate", "--force", "--skip-vectors"]) == 0
    assert count(connect(db_path)) == 1
