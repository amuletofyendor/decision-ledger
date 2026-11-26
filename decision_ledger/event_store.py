from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import time
import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .db import connect
from .model import new_id, now_iso, parse_datetime, parse_expected_by
from .repository import (
    Ledger,
    validate_artifact_association_relation,
    validate_artifact_type,
    validate_association_relation,
    validate_record_kind,
    validate_validation_state,
    validate_wip_area,
    validate_wip_status,
)


DEFAULT_LEDGER_HOME = Path.home() / ".decision-ledger"
STALE_MARKER = "projection-stale.jsonl"
COMMIT_ATTEMPTS = 3
COMMIT_RETRY_DELAY_SECONDS = 0.5


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

    def append(self, **fields: Any) -> dict[str, Any]:
        """Build an event and write it to its subject's JSONL file.

        Args:
            **fields: The keyword arguments accepted by ``build``.

        Returns:
            The written event.
        """
        event = self.build(**fields)
        self.write(event)
        return event

    def build(
        self,
        *,
        subject: str,
        event_type: str,
        record_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any],
        created_by: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        resolved_entity_id = entity_id or record_id
        if resolved_entity_id is None:
            raise ValueError("event entity id is required")
        event = {
            "event_id": event_id or new_id("evt"),
            "event_type": event_type,
            "subject": subject,
            "created_at": created_at or event_now_iso(),
            "created_by": created_by,
            "note": note,
            "payload": payload,
        }
        if entity_type is not None:
            event["entity_type"] = entity_type
            event["entity_id"] = resolved_entity_id
        if record_id is not None:
            event["record_id"] = record_id
        self.event_path(subject)
        return event

    def write(self, event: dict[str, Any]) -> None:
        """Append one event line atomically and durably.

        The line goes out in a single ``os.write`` on an ``O_APPEND`` descriptor under an
        exclusive ``flock``, then ``fsync``, so concurrent writers cannot interleave lines.

        Args:
            event: An event from ``build``.

        Raises:
            OSError: When the file cannot be written.
        """
        path = self.event_path(event["subject"])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)

    def event_path(self, subject: str) -> Path:
        parts = subject.split(".")
        if not parts or any(not part or "/" in part or "\\" in part for part in parts):
            raise ValueError(f"invalid subject for event path: {subject}")
        return self.events_dir.joinpath(*parts[:-1], parts[-1] + ".jsonl")

    def write_artifact(self, *, subject: str, artifact_id: str, extension: str, content: bytes) -> str:
        parts = subject.split(".")
        if not parts or any(not part or "/" in part or "\\" in part for part in parts):
            raise ValueError(f"invalid subject for artifact path: {subject}")
        normalized_extension = normalize_extension(extension)
        relative_path = Path("artifacts").joinpath(*parts, artifact_id + normalized_extension)
        path = self.home / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return relative_path.as_posix()

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
        self.write_warnings: list[str] = []
        self.last_rebuild_skipped: list[dict[str, Any]] = []

    def _commit(self) -> None:
        self.conn.commit()

    def _commit_event(self, event: dict[str, Any]) -> None:
        """Apply an event to the projection, write it to JSONL, then commit.

        A failure before the JSONL write leaves nothing behind, so the caller may retry. Once the
        event is written it is canonical: a commit that still fails after retries is reported in
        ``write_warnings`` and in the stale marker, not raised, so the caller does not retry and
        duplicate it.

        Args:
            event: An event from ``EventStore.build``.

        Raises:
            Exception: Any error from applying or writing the event, after rolling back.
        """
        try:
            apply_event(self.conn, event)
            self.event_store.write(event)
        except BaseException:
            self.conn.rollback()
            raise
        last_error: sqlite3.Error | None = None
        for attempt in range(COMMIT_ATTEMPTS):
            try:
                self._commit()
                return
            except sqlite3.Error as exc:
                last_error = exc
                if attempt + 1 < COMMIT_ATTEMPTS:
                    time.sleep(COMMIT_RETRY_DELAY_SECONDS)
        try:
            self.conn.rollback()
        except sqlite3.Error:
            pass
        self._mark_stale(event, last_error)

    def _mark_stale(self, event: dict[str, Any], error: sqlite3.Error | None) -> None:
        marker = self.event_store.home / STALE_MARKER
        entry = {"event_id": event["event_id"], "subject": event["subject"], "error": str(error)}
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        warning = (
            f"Event {event['event_id']} was recorded in the event log but the SQLite projection "
            f"could not be updated ({error}). Do not retry; run a rebuild to bring the projection up to date."
        )
        self.write_warnings.append(warning)
        print(warning, file=sys.stderr)

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
        validation_state: str = "unvalidated",
    ) -> str:
        validate_record_kind(kind)
        validate_validation_state(validation_state)
        record_id = new_id("rec")
        created_at = now_iso()
        event = self.event_store.build(
            subject=subject,
            event_type="created",
            record_id=record_id,
            created_by=created_by,
            payload={
                "kind": kind,
                "status": status,
                "validation_state": validation_state,
                "summary": summary,
                "body": body,
                "created_at": created_at,
                "created_by": created_by,
                "tags": tags or [],
                "related_subjects": related_subjects or [],
                "export_visibility": export_visibility,
            },
        )
        self._commit_event(event)
        return record_id

    def validate_record(
        self,
        *,
        record_id: str,
        validation_state: str,
        note: str | None = None,
        validated_by: str | None = None,
        validated_at: str | None = None,
    ) -> None:
        validate_validation_state(validation_state)
        record = self.require_record_dict(record_id)
        timestamp = parse_datetime(validated_at) if validated_at else now_iso()
        event = self.event_store.build(
            subject=record["subject"],
            event_type="validation_changed",
            record_id=record_id,
            created_by=validated_by,
            note=note,
            payload={
                "validation_state": validation_state,
                "validated_at": timestamp,
                "validated_by": validated_by,
                "validation_note": note,
            },
        )
        self._commit_event(event)

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
        event = self.event_store.build(
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
        self._commit_event(event)
        return evidence_id

    def add_artifact(
        self,
        *,
        subject: str,
        artifact_type: str,
        content: bytes,
        extension: str,
        content_type: str | None = None,
        label: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        source_uri: str | None = None,
        record_id: str | None = None,
        tags: list[str] | None = None,
        related_subjects: list[str] | None = None,
        created_by: str | None = None,
        export_visibility: str = "private",
    ) -> dict[str, Any]:
        validate_artifact_type(artifact_type)
        if record_id:
            record = self.require_record_dict(record_id)
            subject = record["subject"]
        else:
            record_summary = summary or label or f"{artifact_type.upper()} artifact"
            record_body = body or f"Captured {artifact_type} artifact: {record_summary}"
            record_id = self.add_record(
                subject=subject,
                kind="note",
                status="active",
                summary=record_summary,
                body=record_body,
                created_by=created_by,
                tags=tags or ["artifact", artifact_type],
                related_subjects=related_subjects or [],
                export_visibility=export_visibility,
                validation_state="unvalidated",
            )
        artifact_id = new_id("art")
        resolved_content_type = content_type or infer_content_type(extension, artifact_type)
        storage_path = self.event_store.write_artifact(
            subject=subject,
            artifact_id=artifact_id,
            extension=extension,
            content=content,
        )
        created_at = now_iso()
        event = self.event_store.build(
            subject=subject,
            event_type="artifact_added",
            record_id=record_id,
            created_by=created_by,
            payload={
                "artifact_id": artifact_id,
                "type": artifact_type,
                "content_type": resolved_content_type,
                "storage_path": storage_path,
                "label": label,
                "summary": summary,
                "source_uri": source_uri,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "created_at": created_at,
                "created_by": created_by,
                "export_visibility": export_visibility,
            },
        )
        self._commit_event(event)
        return {
            "id": artifact_id,
            "record_id": record_id,
            "subject": subject,
            "type": artifact_type,
            "content_type": resolved_content_type,
            "storage_path": storage_path,
            "url": f"/artifacts/{artifact_id}/content",
        }

    def associate_artifact(
        self,
        *,
        record_id: str,
        artifact_id: str,
        relation: str,
        note: str | None = None,
        strength: float | None = None,
        source: str = "manual",
        created_by: str | None = None,
    ) -> str:
        record = self.require_record_dict(record_id)
        self.projection.require_artifact(artifact_id)
        validate_artifact_association_relation(relation)
        association_id = new_id("asc")
        event = self.event_store.build(
            subject=record["subject"],
            event_type="artifact_associated",
            record_id=record_id,
            created_by=created_by,
            note=note,
            payload={
                "association_id": association_id,
                "record_id": record_id,
                "artifact_id": artifact_id,
                "relation": relation,
                "note": note,
                "strength": strength,
                "source": source,
            },
        )
        self._commit_event(event)
        return association_id

    def save_view(
        self,
        *,
        subject: str,
        title: str,
        query: dict[str, Any],
        created_by: str | None = None,
        export_visibility: str = "private",
    ) -> dict[str, Any]:
        view_id = new_id("view")
        created_at = now_iso()
        event = self.event_store.build(
            subject=subject,
            event_type="view_saved",
            record_id=view_id,
            created_by=created_by,
            payload={
                "view_id": view_id,
                "title": title,
                "query": query,
                "created_at": created_at,
                "created_by": created_by,
                "export_visibility": export_visibility,
            },
        )
        self._commit_event(event)
        return {
            "id": view_id,
            "subject": subject,
            "title": title,
            "query": query,
            "created_at": created_at,
            "created_by": created_by,
            "export_visibility": export_visibility,
            "url": f"/saved-views/{view_id}.html",
        }

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
        validate_association_relation(relation)
        association_id = new_id("asc")
        event = self.event_store.build(
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
        self._commit_event(event)
        return association_id

    def add_work_item(
        self,
        *,
        area: str,
        subject: str,
        title: str,
        body: str | None = None,
        expected_by: str | None = None,
        created_by: str | None = None,
        export_visibility: str = "private",
    ) -> str:
        validate_wip_area(area)
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("work item title is required")
        work_item_id = new_id("wip")
        created_at = now_iso()
        event = self.event_store.build(
            subject=subject,
            event_type="work_item_created",
            entity_type="work_item",
            entity_id=work_item_id,
            created_by=created_by,
            payload={
                "work_item_id": work_item_id,
                "area": area,
                "title": normalized_title,
                "body": body,
                "status": "open",
                "expected_by": parse_expected_by(expected_by) if expected_by else None,
                "created_at": created_at,
                "created_by": created_by,
                "export_visibility": export_visibility,
            },
        )
        self._commit_event(event)
        return work_item_id

    def update_work_item(
        self,
        *,
        work_item_id: str,
        changes: dict[str, Any],
        updated_by: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        work_item = self.projection.get_work_item(work_item_id)
        if work_item is None:
            raise ValueError(f"work item not found: {work_item_id}")
        allowed = {"area", "subject", "title", "body", "status", "expected_by", "export_visibility"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"unknown work item fields: {', '.join(unknown)}")
        if not changes:
            raise ValueError("at least one work item field must be updated")
        normalized = dict(changes)
        if "area" in normalized:
            validate_wip_area(normalized["area"])
        if "status" in normalized:
            validate_wip_status(normalized["status"])
        if "title" in normalized:
            normalized["title"] = str(normalized["title"]).strip()
            if not normalized["title"]:
                raise ValueError("work item title is required")
        if "subject" in normalized:
            normalized["subject"] = str(normalized["subject"]).strip()
            if not normalized["subject"]:
                raise ValueError("work item subject is required")
        if normalized.get("expected_by"):
            normalized["expected_by"] = parse_expected_by(normalized["expected_by"])
        event_subject = normalized.get("subject", work_item["subject"])
        event = self.event_store.build(
            subject=event_subject,
            event_type="work_item_updated",
            entity_type="work_item",
            entity_id=work_item_id,
            created_by=updated_by,
            note=note,
            payload={
                "work_item_id": work_item_id,
                "changes": normalized,
                "updated_by": updated_by,
            },
        )
        self._commit_event(event)
        updated = self.projection.get_work_item(work_item_id)
        assert updated is not None
        return updated

    def link_work_item_record(
        self,
        *,
        work_item_id: str,
        record_id: str,
        relation: str = "associated_with",
        note: str | None = None,
        created_by: str | None = None,
    ) -> str:
        work_item = self.projection.get_work_item(work_item_id)
        if work_item is None:
            raise ValueError(f"work item not found: {work_item_id}")
        self.projection.require_record(record_id)
        validate_artifact_association_relation(relation)
        link_id = new_id("lnk")
        event = self.event_store.build(
            subject=work_item["subject"],
            event_type="work_item_record_linked",
            entity_type="work_item",
            entity_id=work_item_id,
            created_by=created_by,
            note=note,
            payload={
                "link_id": link_id,
                "work_item_id": work_item_id,
                "record_id": record_id,
                "relation": relation,
                "note": note,
            },
        )
        self._commit_event(event)
        return link_id

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
        event = self.event_store.build(
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
        self._commit_event(event)

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
        self.conn, self.last_rebuild_skipped = rebuild_projection(self.db_path, self.event_store)
        self.projection = Ledger(self.conn)
        marker = self.event_store.home / STALE_MARKER
        if marker.exists():
            marker.unlink()

    def rebuild_vectors(self, *, fail_soft: bool = True) -> dict[str, Any]:
        return self.projection.rebuild_vectors(fail_soft=fail_soft)

    def require_record_dict(self, record_id: str) -> dict[str, Any]:
        record = self.projection.get_record(record_id)
        if record is None:
            raise ValueError(f"record not found: {record_id}")
        return record

    def __getattr__(self, name: str) -> Any:
        return getattr(self.projection, name)


def rebuild_projection(db_path: str | Path, event_store: EventStore) -> tuple[sqlite3.Connection, list[dict[str, Any]]]:
    """Recreate the projection from the event files.

    Args:
        db_path: Projection path.
        event_store: The canonical event store.

    Returns:
        The new connection and the events that could not be applied, with their errors.
    """
    path = Path(db_path).expanduser().resolve()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = connect(path)
    skipped = replay_events(conn, event_store)
    return conn, skipped


def replay_events(conn: sqlite3.Connection, event_store: EventStore) -> list[dict[str, Any]]:
    """Apply every event once, each inside a savepoint, and commit.

    An event that cannot be applied is rolled back on its own, reported, and skipped, so one bad
    event cannot block every rebuild.

    Args:
        conn: Projection connection with no open transaction.
        event_store: The canonical event store.

    Returns:
        The skipped events as ``{event_id, event_type, subject, error}``.
    """
    seen_event_ids: set[str] = set()
    skipped: list[dict[str, Any]] = []
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        for event in event_store.iter_events():
            event_id = event["event_id"]
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            conn.execute("SAVEPOINT replay_event")
            try:
                apply_event(conn, event)
            except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT replay_event")
                skipped.append({
                    "event_id": event_id,
                    "event_type": event.get("event_type"),
                    "subject": event.get("subject"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            conn.execute("RELEASE SAVEPOINT replay_event")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    for item in skipped:
        print(f"rebuild skipped event {item['event_id']}: {item['error']}", file=sys.stderr)
    return skipped


def apply_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    event_type = event["event_type"]
    if event_type == "created":
        apply_record_created(conn, event)
    elif event_type == "evidence_added":
        apply_evidence_added(conn, event)
    elif event_type == "artifact_added":
        apply_artifact_added(conn, event)
    elif event_type == "artifact_associated":
        apply_artifact_associated(conn, event)
    elif event_type == "view_saved":
        apply_view_saved(conn, event)
    elif event_type == "associated":
        apply_associated(conn, event)
    elif event_type == "superseded":
        apply_superseded(conn, event)
    elif event_type == "validation_changed":
        apply_validation_changed(conn, event)
    elif event_type == "work_item_created":
        apply_work_item_created(conn, event)
    elif event_type == "work_item_updated":
        apply_work_item_updated(conn, event)
    elif event_type == "work_item_record_linked":
        apply_work_item_record_linked(conn, event)
    else:
        raise ValueError(f"unknown event type: {event_type}")


def apply_record_created(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_record_kind(payload["kind"])
    created_at = payload.get("created_at") or event["created_at"]
    conn.execute(
        """
        INSERT OR IGNORE INTO records (
          id, subject, kind, status, validation_state, summary, body, created_at, created_by,
          updated_at, valid_from, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["record_id"],
            event["subject"],
            payload["kind"],
            payload["status"],
            payload.get("validation_state", "unvalidated"),
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


def apply_artifact_added(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_artifact_type(payload["type"])
    conn.execute(
        """
        INSERT OR IGNORE INTO artifacts (
          id, record_id, subject, type, content_type, storage_path, label, summary,
          source_uri, sha256, size_bytes, created_at, created_by, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["artifact_id"],
            event["record_id"],
            event["subject"],
            payload["type"],
            payload["content_type"],
            payload["storage_path"],
            payload.get("label"),
            payload.get("summary"),
            payload.get("source_uri"),
            payload["sha256"],
            payload["size_bytes"],
            payload.get("created_at") or event["created_at"],
            payload.get("created_by") or event.get("created_by"),
            payload.get("export_visibility", "private"),
        ),
    )
    insert_projection_event(conn, event, "artifact_added")


def apply_artifact_associated(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_artifact_association_relation(payload["relation"])
    conn.execute(
        """
        INSERT OR IGNORE INTO record_artifact_associations (
          id, record_id, artifact_id, relation, strength, note, source,
          created_at, created_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["association_id"],
            payload["record_id"],
            payload["artifact_id"],
            payload["relation"],
            payload.get("strength"),
            payload.get("note"),
            payload.get("source", "manual"),
            event["created_at"],
            event.get("created_by"),
        ),
    )
    insert_projection_event(conn, event, "artifact_associated")


def apply_view_saved(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    conn.execute(
        """
        INSERT OR REPLACE INTO saved_views (
          id, subject, title, query_json, created_at, created_by, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["view_id"],
            event["subject"],
            payload["title"],
            json.dumps(payload["query"], sort_keys=True),
            payload.get("created_at") or event["created_at"],
            payload.get("created_by") or event.get("created_by"),
            payload.get("export_visibility", "private"),
        ),
    )


def apply_associated(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_association_relation(payload["relation"])
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


def apply_validation_changed(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_validation_state(payload["validation_state"])
    conn.execute(
        """
        UPDATE records
        SET validation_state = ?,
            validated_at = ?,
            validated_by = ?,
            validation_note = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            payload["validation_state"],
            parse_datetime(payload["validated_at"]) if payload.get("validated_at") else event["created_at"],
            payload.get("validated_by") or event.get("created_by"),
            payload.get("validation_note"),
            event["created_at"],
            event["record_id"],
        ),
    )
    insert_projection_event(conn, event, "validation_changed")


def apply_work_item_created(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_wip_area(payload["area"])
    validate_wip_status(payload.get("status", "open"))
    expected_by = parse_expected_by(payload["expected_by"]) if payload.get("expected_by") else None
    created_at = payload.get("created_at") or event["created_at"]
    conn.execute(
        """
        INSERT OR IGNORE INTO work_items (
          id, area, subject, title, body, status, expected_by, created_at, created_by,
          updated_at, export_visibility
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["work_item_id"],
            payload["area"],
            event["subject"],
            payload["title"],
            payload.get("body"),
            payload.get("status", "open"),
            expected_by,
            created_at,
            payload.get("created_by") or event.get("created_by"),
            created_at,
            payload.get("export_visibility", "private"),
        ),
    )
    insert_work_item_projection_event(conn, event, "created")


def apply_work_item_updated(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    work_item_id = payload["work_item_id"]
    changes = payload["changes"]
    columns = {
        "area": "area",
        "subject": "subject",
        "title": "title",
        "body": "body",
        "status": "status",
        "expected_by": "expected_by",
        "export_visibility": "export_visibility",
    }
    assignments: list[str] = []
    params: list[Any] = []
    for field, column in columns.items():
        if field not in changes:
            continue
        value = changes[field]
        if field == "area":
            validate_wip_area(value)
        elif field == "status":
            validate_wip_status(value)
        elif field == "expected_by" and value:
            value = parse_expected_by(value)
        assignments.append(f"{column} = ?")
        params.append(value)
    if not assignments:
        raise ValueError("work item update contains no fields")
    if changes.get("status") == "closed":
        assignments.extend(["closed_at = ?", "closed_by = ?"])
        params.extend([event["created_at"], payload.get("updated_by") or event.get("created_by")])
    elif changes.get("status") == "open":
        assignments.extend(["closed_at = NULL", "closed_by = NULL"])
    assignments.append("updated_at = ?")
    params.extend([event["created_at"], work_item_id])
    conn.execute(
        f"UPDATE work_items SET {', '.join(assignments)} WHERE id = ?",
        params,
    )
    insert_work_item_projection_event(conn, event, "updated")


def apply_work_item_record_linked(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = event["payload"]
    validate_artifact_association_relation(payload["relation"])
    conn.execute(
        """
        INSERT OR IGNORE INTO work_item_record_links (
          id, work_item_id, record_id, relation, note, created_at, created_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["link_id"],
            payload["work_item_id"],
            payload["record_id"],
            payload["relation"],
            payload.get("note"),
            event["created_at"],
            event.get("created_by"),
        ),
    )
    insert_work_item_projection_event(conn, event, "linked")


def insert_work_item_projection_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    event_type: str,
) -> None:
    work_item_id = event["payload"]["work_item_id"]
    conn.execute(
        """
        INSERT OR IGNORE INTO work_item_events (
          id, work_item_id, event_type, event_at, event_by, note, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_id"],
            work_item_id,
            event_type,
            event["created_at"],
            event.get("created_by"),
            event.get("note"),
            json.dumps(event.get("payload"), sort_keys=True),
        ),
    )


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


def normalize_extension(extension: str) -> str:
    normalized = extension.lower().strip()
    if not normalized:
        raise ValueError("artifact extension is required")
    if not normalized.startswith("."):
        normalized = "." + normalized
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError(f"invalid artifact extension: {extension}")
    return normalized


def infer_content_type(extension: str, artifact_type: str) -> str:
    normalized = normalize_extension(extension)
    if artifact_type == "html":
        return "text/html; charset=utf-8"
    if artifact_type in {"snippet", "pseudocode", "markdown", "json", "yaml", "text"}:
        return text_artifact_content_type(artifact_type)
    guessed, _encoding = mimetypes.guess_type("artifact" + normalized)
    return guessed or "application/octet-stream"


def text_artifact_content_type(artifact_type: str) -> str:
    return {
        "snippet": "text/plain; charset=utf-8",
        "pseudocode": "text/plain; charset=utf-8",
        "markdown": "text/markdown; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "yaml": "application/yaml; charset=utf-8",
        "text": "text/plain; charset=utf-8",
    }.get(artifact_type, "text/plain; charset=utf-8")
