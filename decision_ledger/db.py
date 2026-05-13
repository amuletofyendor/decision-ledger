from __future__ import annotations

import sqlite3
from pathlib import Path

from .model import ARTIFACT_TYPES, ASSOCIATION_RELATIONS, RECORD_KINDS


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "001_initial.sql"
DEFAULT_DB_PATH = Path.home() / ".decision-ledger" / "ledger.sqlite"
RECORD_EVENT_TYPES = (
    "created",
    "updated",
    "status_changed",
    "superseded",
    "withdrawn",
    "associated",
    "evidence_added",
    "artifact_added",
    "artifact_associated",
    "validation_changed",
    "tag_added",
    "export_visibility_changed",
)


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
    migrate_record_kind_constraint(conn)
    migrate_artifact_type_constraint(conn)
    migrate_record_association_relation_constraint(conn)
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


def migrate_record_kind_constraint(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'records'
        """
    ).fetchone()
    table_sql = row["sql"] if row else ""
    if not table_sql or all(f"'{kind}'" in table_sql for kind in RECORD_KINDS):
        return

    kind_values = ", ".join(f"'{kind}'" for kind in RECORD_KINDS)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            f"""
            DROP TRIGGER IF EXISTS records_ai;
            DROP TRIGGER IF EXISTS records_ad;
            DROP TRIGGER IF EXISTS records_au;

            CREATE TABLE records_new (
              id TEXT PRIMARY KEY,
              subject TEXT NOT NULL,
              kind TEXT NOT NULL CHECK (
                kind IN ({kind_values})
              ),
              status TEXT NOT NULL CHECK (
                status IN ('active', 'proposed', 'accepted', 'rejected', 'superseded', 'withdrawn', 'resolved', 'archived')
              ),
              validation_state TEXT NOT NULL DEFAULT 'unvalidated' CHECK (
                validation_state IN ('unvalidated', 'partially_validated', 'validated', 'contested', 'invalidated')
              ),
              summary TEXT,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT,
              validated_at TEXT,
              validated_by TEXT,
              validation_note TEXT,
              updated_at TEXT,
              valid_from TEXT,
              valid_until TEXT,
              export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
                export_visibility IN ('private', 'internal', 'shareable', 'public')
              )
            );

            INSERT INTO records_new (
              rowid, id, subject, kind, status, validation_state, summary, body,
              created_at, created_by, validated_at, validated_by, validation_note,
              updated_at, valid_from, valid_until, export_visibility
            )
            SELECT
              rowid, id, subject, kind, status, validation_state, summary, body,
              created_at, created_by, validated_at, validated_by, validation_note,
              updated_at, valid_from, valid_until, export_visibility
            FROM records;

            DROP TABLE records;
            ALTER TABLE records_new RENAME TO records;

            CREATE INDEX IF NOT EXISTS idx_records_subject ON records(subject);
            CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
            CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind);
            CREATE INDEX IF NOT EXISTS idx_records_created_at ON records(created_at);
            CREATE INDEX IF NOT EXISTS idx_records_validation_state ON records(validation_state);

            CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
              INSERT INTO records_fts(rowid, subject, summary, body)
              VALUES (new.rowid, new.subject, new.summary, new.body);
            END;

            CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
              INSERT INTO records_fts(records_fts, rowid, subject, summary, body)
              VALUES ('delete', old.rowid, old.subject, old.summary, old.body);
            END;

            CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
              INSERT INTO records_fts(records_fts, rowid, subject, summary, body)
              VALUES ('delete', old.rowid, old.subject, old.summary, old.body);
              INSERT INTO records_fts(rowid, subject, summary, body)
              VALUES (new.rowid, new.subject, new.summary, new.body);
            END;

            INSERT INTO records_fts(records_fts) VALUES ('rebuild');
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate_record_events_validation_type(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'record_events'
        """
    ).fetchone()
    table_sql = row["sql"] if row else ""
    if not table_sql or all(f"'{event_type}'" in table_sql for event_type in RECORD_EVENT_TYPES):
        return

    event_values = ",\n              ".join(f"'{event_type}'" for event_type in RECORD_EVENT_TYPES)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        f"""
        ALTER TABLE record_events RENAME TO record_events_old;

        CREATE TABLE record_events (
          id TEXT PRIMARY KEY,
          record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL CHECK (
            event_type IN (
              {event_values}
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


def migrate_artifact_type_constraint(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'artifacts'
        """
    ).fetchone()
    table_sql = row["sql"] if row else ""
    if not table_sql or all(f"'{artifact_type}'" in table_sql for artifact_type in ARTIFACT_TYPES):
        return

    artifact_values = ", ".join(f"'{artifact_type}'" for artifact_type in ARTIFACT_TYPES)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            f"""
            ALTER TABLE artifacts RENAME TO artifacts_old;

            CREATE TABLE artifacts (
              id TEXT PRIMARY KEY,
              record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
              subject TEXT NOT NULL,
              type TEXT NOT NULL CHECK (
                type IN ({artifact_values})
              ),
              content_type TEXT NOT NULL,
              storage_path TEXT NOT NULL UNIQUE,
              label TEXT,
              summary TEXT,
              source_uri TEXT,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT,
              export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
                export_visibility IN ('private', 'internal', 'shareable', 'public')
              )
            );

            INSERT INTO artifacts (
              id, record_id, subject, type, content_type, storage_path, label, summary,
              source_uri, sha256, size_bytes, created_at, created_by, export_visibility
            )
            SELECT
              id, record_id, subject, type, content_type, storage_path, label, summary,
              source_uri, sha256, size_bytes, created_at, created_by, export_visibility
            FROM artifacts_old;

            DROP TABLE artifacts_old;

            CREATE INDEX IF NOT EXISTS idx_artifacts_record_id ON artifacts(record_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_subject ON artifacts(subject);
            CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);
            CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at);
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate_record_association_relation_constraint(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'record_associations'
        """
    ).fetchone()
    table_sql = row["sql"] if row else ""
    if not table_sql or all(f"'{relation}'" in table_sql for relation in ASSOCIATION_RELATIONS):
        return

    relation_values = ",\n                ".join(f"'{relation}'" for relation in ASSOCIATION_RELATIONS)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            f"""
            ALTER TABLE record_associations RENAME TO record_associations_old;

            CREATE TABLE record_associations (
              id TEXT PRIMARY KEY,
              from_record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
              to_record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
              relation TEXT NOT NULL CHECK (
                relation IN (
                {relation_values}
                )
              ),
              strength REAL,
              note TEXT,
              source TEXT NOT NULL DEFAULT 'manual' CHECK (
                source IN ('manual', 'agent', 'import', 'inferred')
              ),
              created_at TEXT NOT NULL,
              created_by TEXT,
              UNIQUE (from_record_id, to_record_id, relation)
            );

            INSERT INTO record_associations (
              id, from_record_id, to_record_id, relation, strength, note, source, created_at, created_by
            )
            SELECT id, from_record_id, to_record_id, relation, strength, note, source, created_at, created_by
            FROM record_associations_old;

            DROP TABLE record_associations_old;

            CREATE INDEX IF NOT EXISTS idx_record_associations_from ON record_associations(from_record_id);
            CREATE INDEX IF NOT EXISTS idx_record_associations_to ON record_associations(to_record_id);
            CREATE INDEX IF NOT EXISTS idx_record_associations_relation ON record_associations(relation);
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
