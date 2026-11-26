from __future__ import annotations

import threading
import time
from pathlib import Path

from decision_ledger.db import BUSY_TIMEOUT_MS, connect, wal_supported


def test_connect_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.sqlite")

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_second_writer_waits_for_the_first(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    first = connect(db_path)
    second = connect(db_path)
    ready = threading.Event()

    def hold_write_lock() -> None:
        holder = connect(db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO saved_views_lock_probe(value) VALUES (1)")
        ready.set()
        time.sleep(0.5)
        holder.commit()
        holder.close()

    first.execute("CREATE TABLE saved_views_lock_probe(value INTEGER)")
    first.commit()
    thread = threading.Thread(target=hold_write_lock)
    thread.start()
    ready.wait(5)

    second.execute("INSERT INTO saved_views_lock_probe(value) VALUES (2)")
    second.commit()
    thread.join()

    assert [row[0] for row in first.execute("SELECT value FROM saved_views_lock_probe ORDER BY value")] == [1, 2]


def test_wal_is_not_used_on_windows_mounts() -> None:
    assert wal_supported(Path("/home/user/.decision-ledger/ledger.sqlite")) is True
    assert wal_supported(Path("/mnt/c/Users/user/.decision-ledger/ledger.sqlite")) is False
