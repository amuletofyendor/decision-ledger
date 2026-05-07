from __future__ import annotations

import json
import sqlite3
from typing import Any

from .model import CURRENT_STATUSES, OBSOLETE_STATUSES, new_id, now_iso, parse_datetime


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

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
        tags = tags or []
        related_subjects = related_subjects or []

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO records (
                  id, subject, kind, status, summary, body, created_at, created_by,
                  updated_at, valid_from, export_visibility
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    subject,
                    kind,
                    status,
                    summary,
                    body,
                    created_at,
                    created_by,
                    created_at,
                    created_at,
                    export_visibility,
                ),
            )
            for tag in tags:
                self.conn.execute(
                    "INSERT OR IGNORE INTO record_tags(record_id, tag) VALUES (?, ?)",
                    (record_id, tag),
                )
                self.add_event(record_id, "tag_added", created_by, payload={"tag": tag})
            for related in related_subjects:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO record_subjects(record_id, subject, relation)
                    VALUES (?, ?, 'related')
                    """,
                    (record_id, related),
                )
            self.add_event(record_id, "created", created_by)
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
        self.require_record(record_id)
        evidence_id = new_id("evd")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO evidence (
                  id, record_id, type, uri, line, label, note, observed_at, export_visibility
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    record_id,
                    evidence_type,
                    uri,
                    line,
                    label,
                    note,
                    parse_datetime(observed_at) if observed_at else None,
                    export_visibility,
                ),
            )
            self.add_event(
                record_id,
                "evidence_added",
                created_by,
                note=note,
                payload={"evidence_id": evidence_id, "type": evidence_type, "uri": uri},
            )
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
        self.require_record(from_record_id)
        self.require_record(to_record_id)
        association_id = new_id("asc")
        created_at = now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO record_associations (
                  id, from_record_id, to_record_id, relation, strength, note,
                  source, created_at, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    association_id,
                    from_record_id,
                    to_record_id,
                    relation,
                    strength,
                    note,
                    source,
                    created_at,
                    created_by,
                ),
            )
            self.add_event(
                from_record_id,
                "associated",
                created_by,
                note=note,
                payload={"to_record_id": to_record_id, "relation": relation},
            )
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
        self.require_record(old_record_id)
        self.require_record(replacement_record_id)
        timestamp = parse_datetime(valid_until) if valid_until else now_iso()
        with self.conn:
            self.associate(
                from_record_id=replacement_record_id,
                to_record_id=old_record_id,
                relation="supersedes",
                note=note,
                created_by=created_by,
            )
            self.conn.execute(
                """
                UPDATE records
                SET status = 'superseded', valid_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, now_iso(), old_record_id),
            )
            self.add_event(old_record_id, "superseded", created_by, note=note)

    def supersede_subject_before(
        self,
        *,
        subject: str,
        before: str,
        replacement_record_id: str,
        note: str | None = None,
        created_by: str | None = None,
    ) -> list[str]:
        self.require_record(replacement_record_id)
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

    def list_records(
        self,
        *,
        subject: str | None = None,
        status: str | None = None,
        include_obsolete: bool = False,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject:
            clauses.append("(subject = ? OR subject LIKE ?)")
            params.extend([subject, f"{subject}.%"])
        if status:
            clauses.append("status = ?")
            params.append(status)
        elif not include_obsolete:
            placeholders = ",".join("?" for _ in CURRENT_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(CURRENT_STATUSES)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT id, subject, kind, status, summary, created_at
            FROM records
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def search(self, query: str, *, limit: int = 20, include_obsolete: bool = False) -> list[sqlite3.Row]:
        status_clause = "" if include_obsolete else self._current_status_clause("r.status")
        params: list[Any] = [query]
        if not include_obsolete:
            params.extend(CURRENT_STATUSES)
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT r.id, r.subject, r.kind, r.status, r.summary, r.created_at,
                   bm25(records_fts) AS rank
            FROM records_fts
            JOIN records r ON r.rowid = records_fts.rowid
            WHERE records_fts MATCH ?
            {status_clause}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()

    def gather(self, subject: str, *, include_obsolete: bool = False) -> dict[str, Any]:
        namespace_rows = self.list_records(
            subject=subject,
            include_obsolete=include_obsolete,
            limit=500,
        )
        record_ids = [row["id"] for row in namespace_rows]
        associated_rows = self.associated_records(record_ids, include_obsolete=include_obsolete)
        evidence_rows = self.evidence_for_records(record_ids)
        grouped = {
            "current": [dict(row) for row in namespace_rows if row["status"] in CURRENT_STATUSES],
            "obsolete": [dict(row) for row in namespace_rows if row["status"] in OBSOLETE_STATUSES],
            "associated": [dict(row) for row in associated_rows],
            "evidence": [dict(row) for row in evidence_rows],
        }
        return grouped

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "tags": [tag["tag"] for tag in self.conn.execute("SELECT tag FROM record_tags WHERE record_id = ? ORDER BY tag", (record_id,))],
            "related_subjects": [
                dict(item)
                for item in self.conn.execute(
                    "SELECT subject, relation FROM record_subjects WHERE record_id = ? ORDER BY subject",
                    (record_id,),
                )
            ],
            "evidence": [dict(item) for item in self.evidence_for_records([record_id])],
            "associations_out": [
                dict(item)
                for item in self.conn.execute(
                    """
                    SELECT relation, to_record_id AS record_id, note, strength, source, created_at
                    FROM record_associations
                    WHERE from_record_id = ?
                    ORDER BY created_at
                    """,
                    (record_id,),
                )
            ],
            "associations_in": [
                dict(item)
                for item in self.conn.execute(
                    """
                    SELECT relation, from_record_id AS record_id, note, strength, source, created_at
                    FROM record_associations
                    WHERE to_record_id = ?
                    ORDER BY created_at
                    """,
                    (record_id,),
                )
            ],
            "events": [
                dict(item)
                for item in self.conn.execute(
                    """
                    SELECT event_type, event_at, event_by, note, payload_json
                    FROM record_events
                    WHERE record_id = ?
                    ORDER BY event_at
                    """,
                    (record_id,),
                )
            ],
        }

    def require_record(self, record_id: str) -> None:
        if not self.conn.execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone():
            raise ValueError(f"record not found: {record_id}")

    def evidence_for_records(self, record_ids: list[str]) -> list[sqlite3.Row]:
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        return self.conn.execute(
            f"""
            SELECT id, record_id, type, uri, line, label, note, observed_at, export_visibility
            FROM evidence
            WHERE record_id IN ({placeholders})
            ORDER BY observed_at IS NULL, observed_at, id
            """,
            record_ids,
        ).fetchall()

    def associated_records(self, record_ids: list[str], *, include_obsolete: bool = False) -> list[sqlite3.Row]:
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        params: list[Any] = [*record_ids, *record_ids, *record_ids]
        status_clause = "" if include_obsolete else self._current_status_clause("r.status")
        if not include_obsolete:
            params.extend(CURRENT_STATUSES)
        return self.conn.execute(
            f"""
            SELECT DISTINCT r.id, r.subject, r.kind, r.status, r.summary,
                   a.relation, a.note, a.from_record_id, a.to_record_id
            FROM record_associations a
            JOIN records r
              ON (
                (a.from_record_id IN ({placeholders}) AND r.id = a.to_record_id)
                OR
                (a.to_record_id IN ({placeholders}) AND r.id = a.from_record_id)
              )
            WHERE r.id NOT IN ({placeholders})
            {status_clause}
            ORDER BY r.created_at DESC
            """,
            params,
        ).fetchall()

    def add_event(
        self,
        record_id: str,
        event_type: str,
        event_by: str | None,
        *,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        event_id = new_id("evt")
        self.conn.execute(
            """
            INSERT INTO record_events(id, record_id, event_type, event_at, event_by, note, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                record_id,
                event_type,
                now_iso(),
                event_by,
                note,
                json.dumps(payload, sort_keys=True) if payload else None,
            ),
        )
        return event_id

    @staticmethod
    def _current_status_clause(column: str) -> str:
        placeholders = ",".join("?" for _ in CURRENT_STATUSES)
        return f"AND {column} IN ({placeholders})"
