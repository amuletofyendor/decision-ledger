from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .db import connect
from .model import new_id, now_iso, parse_datetime
from .repository import Ledger


DEFAULT_LEDGER_HOME = Path.home() / ".decision-ledger"


@dataclass(frozen=True)
class LedgerPaths:
    home: Path
    db_path: Path
    events_dir: Path


def resolve_ledger_paths(
    *,
    db_path: str | Path | None = None,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
) -> LedgerPaths:
    env_home = os.environ.get("DECISION_LEDGER_HOME")
    env_db = os.environ.get("DECISION_LEDGER_DB")
    cwd_path = Path(cwd or Path.cwd()).resolve()

    explicit_home = Path(home or env_home).expanduser().resolve() if home or env_home else None
    explicit_db = Path(db_path or env_db).expanduser().resolve() if db_path or env_db else None

    if explicit_home is None and explicit_db is not None:
        explicit_home = explicit_db.parent

    if explicit_home is None:
        explicit_home = find_ledger_home(cwd_path)

    if explicit_home is None:
        explicit_home = DEFAULT_LEDGER_HOME

    resolved_db = explicit_db or explicit_home / "ledger.sqlite"
    return LedgerPaths(
        home=explicit_home,
        db_path=resolved_db,
        events_dir=explicit_home / "events",
    )


def find_ledger_home(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        ledger_home = candidate / ".decision-ledger"
        if ledger_home.is_dir():
            return ledger_home.resolve()
    return None


class EventStore:
    def __init__(self, home: str | Path):
        self.home = Path(home).expanduser().resolve()
        self.events_dir = self.home / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        subject: str,
        event_type: str,
        record_id: str,
        payload: dict[str, Any],
        created_by: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": event_id or new_id("evt"),
            "event_type": event_type,
            "record_id": record_id,
            "subject": subject,
            "created_at": created_at or event_now_iso(),
            "created_by": created_by,
            "note": note,
            "payload": payload,
        }
        path = self.event_path(subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        return event

    def event_path(self, subject: str) -> Path:
        parts = subject.split(".")
        if not parts or any(not part or "/" in part or "\\" in part for part in parts):
            raise ValueError(f"invalid subject for event path: {subject}")
        return self.events_dir.joinpath(*parts[:-1], parts[-1] + ".jsonl")

    def has_events(self) -> bool:
        return any(self.events_dir.rglob("*.jsonl"))

    def iter_events(self) -> Iterable[dict[str, Any]]:
        events: list[tuple[str, str, int, dict[str, Any]]] = []
        for path in sorted(self.events_dir.rglob("*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    event = json.loads(stripped)
                    events.append((event["created_at"], str(path), line_number, event))
        for _created_at, _path, _line_number, event in sorted(events):
            yield event


class EventedLedger:
    def __init__(self, conn: sqlite3.Connection, event_store: EventStore, db_path: str | Path):
        self.conn = conn
        self.event_store = event_store
        self.db_path = Path(db_path).expanduser().resolve()
        self.projection = Ledger(conn)

    def add_record(
        self,
        *,
        subject: str,
        kind: str,
        status: str,
        summary: str | None,
        body: str,
        created_by: str | None = None,
        tags: list[str] | None = None,
        related_subjects: list[str] | None = None,
        export_visibility: str = "private",
    ) -> str:
        record_id = new_id("rec")
        created_at = now_iso()
        event = self.event_store.append(
            subject=subject,
            event_type="created",
            record_id=record_id,
            created_by=created_by,
            payload={
                "kind": kind,
                "status": status,
                "summary": summary,
                "body": body,
                "created_at": created_at,
                "created_by": created_by,
                "tags": tags or [],
                "related_subjects": related_subjects or [],
                "export_visibility": export_visibility,
            },
        )
        apply_event(self.conn, event)
        self.conn.commit()
        return record_id

    def add_evidence(
        self,
        *,
        record_id: str,
        evidence_type: str,
        uri: str,
        line: int | None = None,
        label: str | None = None,
        note: str | None = None,
        observed_at: str | None = None,
        export_visibility: str = "private",
        created_by: str | None = None,
    ) -> str:
        record = self.require_record_dict(record_id)
        evidence_id = new_id("evd")
        event = self.event_store.append(
            subject=record["subject"],
            event_type="evidence_added",
            record_id=record_id,
            created_by=created_by,
            note=note,
            payload={
                "evidence_id": evidence_id,
                "type": evidence_type,
                "uri": uri,
                "line": line,
                "label": label,
                "note": note,
                "observed_at": observed_at,
                "export_visibility": export_visibility,
            },
        )
        apply_event(self.conn, event)
        self.conn.commit()
        return evidence_id

    def associate(
        self,
        *,
        from_record_id: str,
        to_record_id: str,
        relation: str,
        note: str | None = None,
        strength: float | None = None,
        source: str = "manual",
        created_by: str | None = None,
    ) -> str:
        from_record = self.require_record_dict(from_record_id)
        self.projection.require_record(to_record_id)
        association_id = new_id("asc")
        event = self.event_store.append(
            subject=from_record["subject"],
            event_type="associated",
            record_id=from_record_id,
            created_by=created_by,
            note=note,
            payload={
                "association_id": association_id,
                "from_record_id": from_record_id,
                "to_record_id": to_record_id,
                "relation": relation,
                "note": note,
                "strength": strength,
                "source": source,
            },
        )
        apply_event(self.conn, event)
        self.conn.commit()
        return association_id

    def supersede_record(
        self,
        *,
        old_record_id: str,
        replacement_record_id: str,
        note: str | None = None,
        created_by: str | None = None,
        valid_until: str | None = None,
    ) -> None:
        old_record = self.require_record_dict(old_record_id)
        self.projection.require_record(replacement_record_id)
        event = self.event_store.append(
            subject=old_record["subject"],
            event_type="superseded",
            record_id=old_record_id,
            created_by=created_by,
            note=note,
            payload={
                "old_record_id": old_record_id,
                "replacement_record_id": replacement_record_id,
                "association_id": new_id("asc"),
                "valid_until": valid_until,
                "note": note,
            },
        )
        apply_event(self.conn, event)
        self.conn.commit()

    def supersede_subject_before(
        self,
        *,
        subject: str,
        before: str,
        replacement_record_id: str,
        note: str | None = None,
        created_by: str | None = None,
    ) -> list[str]:
        self.projection.require_record(replacement_record_id)
        before_iso = parse_datetime(before)
        rows = self.conn.execute(
            """
            SELECT id
            FROM records
            WHERE (subject = ? OR subject LIKE ?)
              AND created_at < ?
              AND status IN ('active', 'proposed', 'accepted', 'resolved')
              AND id <> ?
            ORDER BY created_at
            """,
            (subject, f"{subject}.%", before_iso, replacement_record_id),
        ).fetchall()
        superseded = [row["id"] for row in rows]
        for old_record_id in superseded:
            self.supersede_record(
                old_record_id=old_record_id,
                replacement_record_id=replacement_record_id,
                note=note,
                created_by=created_by,
                valid_until=before_iso,
            )
        return superseded

    def rebuild(self) -> None:
        self.conn.close()
        self.conn = rebuild_projection(self.db_path, self.event_store)
        self.projection = Ledger(self.conn)

    def require_record_dict(self, record_id: str) -> dict[str, Any]:
        record = self.projection.get_record(record_id)
        if record is None:
            raise ValueError(f"record not found: {record_id}")
        return record

    def __getattr__(self, name: str) -> Any:
        return getattr(self.projection, name)


def rebuild_projection(db_path: str | Path, event_store: EventStore) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = connect(path)
    seen_event_ids: set[str] = set()
    with conn:
        for event in event_store.iter_events():
            event_id = event["event_id"]
            if event_id in seen_event_ids:
                continue
            apply_event(conn, event)
            seen_event_ids.add(event_id)
    return conn


def apply_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    event_type = event["event_type"]
    if event_type == "created":
        apply_record_created(conn, event)
    elif event_type == "evidence_added":
        apply_evidence_added(conn, event)
    elif event_type == "associated":
        apply_associated(conn, event)
    elif event_type == "superseded":
        apply_superseded(conn, event)
    else:
        raise ValueError(f"unknown event type: {event_type}")


def apply_record_created(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    created_at = payload.get("created_at") or event["created_at"]
    conn.execute(
        """
        INSERT OR IGNORE INTO records (
          id, subject, kind, status, summary, body, created_at, created_by,
          updated_at, valid_from, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["record_id"],
            event["subject"],
            payload["kind"],
            payload["status"],
            payload.get("summary"),
            payload["body"],
            created_at,
            payload.get("created_by") or event.get("created_by"),
            created_at,
            created_at,
            payload.get("export_visibility", "private"),
        ),
    )
    for tag in payload.get("tags", []):
        conn.execute(
            "INSERT OR IGNORE INTO record_tags(record_id, tag) VALUES (?, ?)",
            (event["record_id"], tag),
        )
    for related in payload.get("related_subjects", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO record_subjects(record_id, subject, relation)
            VALUES (?, ?, 'related')
            """,
            (event["record_id"], related),
        )
    insert_projection_event(conn, event, "created")


def apply_evidence_added(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    conn.execute(
        """
        INSERT OR IGNORE INTO evidence (
          id, record_id, type, uri, line, label, note, observed_at, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["evidence_id"],
            event["record_id"],
            payload["type"],
            payload["uri"],
            payload.get("line"),
            payload.get("label"),
            payload.get("note"),
            parse_datetime(payload["observed_at"]) if payload.get("observed_at") else None,
            payload.get("export_visibility", "private"),
        ),
    )
    insert_projection_event(conn, event, "evidence_added")


def apply_associated(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    conn.execute(
        """
        INSERT OR IGNORE INTO record_associations (
          id, from_record_id, to_record_id, relation, strength, note, source,
          created_at, created_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["association_id"],
            payload["from_record_id"],
            payload["to_record_id"],
            payload["relation"],
            payload.get("strength"),
            payload.get("note"),
            payload.get("source", "manual"),
            event["created_at"],
            event.get("created_by"),
        ),
    )
    insert_projection_event(conn, event, "associated")


def apply_superseded(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    valid_until = parse_datetime(payload["valid_until"]) if payload.get("valid_until") else event["created_at"]
    conn.execute(
        """
        INSERT OR IGNORE INTO record_associations (
          id, from_record_id, to_record_id, relation, strength, note, source,
          created_at, created_by
        )
        VALUES (?, ?, ?, 'supersedes', NULL, ?, 'manual', ?, ?)
        """,
        (
            payload["association_id"],
            payload["replacement_record_id"],
            payload["old_record_id"],
            payload.get("note"),
            event["created_at"],
            event.get("created_by"),
        ),
    )
    conn.execute(
        """
        UPDATE records
        SET status = 'superseded', valid_until = ?, updated_at = ?
        WHERE id = ?
        """,
        (valid_until, event["created_at"], payload["old_record_id"]),
    )
    insert_projection_event(conn, event, "superseded")


def insert_projection_event(conn: sqlite3.Connection, event: dict[str, Any], projection_type: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO record_events(id, record_id, event_type, event_at, event_by, note, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_id"],
            event["record_id"],
            projection_type,
            event["created_at"],
            event.get("created_by"),
            event.get("note"),
            json.dumps(event["payload"], sort_keys=True),
        ),
    )


def event_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")
