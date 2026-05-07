from __future__ import annotations

import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "001_initial.sql"
DEFAULT_DB_PATH = Path.home() / ".decision-ledger" / "ledger.sqlite"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_validation_columns(conn)
    migrate_record_events_validation_type(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_records_validation_state ON records(validation_state)")
    conn.commit()


def migrate_validation_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(records)")}
    if "validation_state" not in columns:
        conn.execute(
            """
            ALTER TABLE records
            ADD COLUMN validation_state TEXT NOT NULL DEFAULT 'unvalidated'
            CHECK (validation_state IN ('unvalidated', 'partially_validated', 'validated', 'contested', 'invalidated'))
            """
        )
    if "validated_at" not in columns:
        conn.execute("ALTER TABLE records ADD COLUMN validated_at TEXT")
    if "validated_by" not in columns:
        conn.execute("ALTER TABLE records ADD COLUMN validated_by TEXT")
    if "validation_note" not in columns:
        conn.execute("ALTER TABLE records ADD COLUMN validation_note TEXT")


def migrate_record_events_validation_type(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'record_events'
        """
    ).fetchone()
    if not row or "validation_changed" in (row["sql"] or ""):
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        ALTER TABLE record_events RENAME TO record_events_old;

        CREATE TABLE record_events (
          id TEXT PRIMARY KEY,
          record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL CHECK (
            event_type IN (
              'created',
              'updated',
              'status_changed',
              'superseded',
              'withdrawn',
              'associated',
              'evidence_added',
              'validation_changed',
              'tag_added',
              'export_visibility_changed'
            )
          ),
          event_at TEXT NOT NULL,
          event_by TEXT,
          note TEXT,
          payload_json TEXT
        );

        INSERT INTO record_events(id, record_id, event_type, event_at, event_by, note, payload_json)
        SELECT id, record_id, event_type, event_at, event_by, note, payload_json
        FROM record_events_old;

        DROP TABLE record_events_old;

        CREATE INDEX IF NOT EXISTS idx_record_events_record_id ON record_events(record_id);
        CREATE INDEX IF NOT EXISTS idx_record_events_event_type ON record_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_record_events_event_at ON record_events(event_at);
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")
