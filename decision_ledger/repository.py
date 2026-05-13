from __future__ import annotations

import json
import sqlite3
from typing import Any

from .model import CURRENT_STATUSES, OBSOLETE_STATUSES, RECORD_KINDS, RECORD_STATUSES, VALIDATION_STATES, new_id, now_iso, parse_datetime
from .vector_search import (
    Embedder,
    rebuild_record_vectors,
    rebuild_record_vectors_if_available,
    vector_search_records,
    vector_search_records_if_available,
)


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
        validation_state: str = "unvalidated",
    ) -> str:
        validate_record_kind(kind)
        validate_validation_state(validation_state)
        record_id = new_id("rec")
        created_at = now_iso()
        tags = tags or []
        related_subjects = related_subjects or []

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO records (
                  id, subject, kind, status, validation_state, summary, body, created_at, created_by,
                  updated_at, valid_from, export_visibility
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    subject,
                    kind,
                    status,
                    validation_state,
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

    def validate_record(
        self,
        *,
        record_id: str,
        validation_state: str,
        note: str | None = None,
        validated_by: str | None = None,
        validated_at: str | None = None,
    ) -> None:
        self.require_record(record_id)
        validate_validation_state(validation_state)
        timestamp = parse_datetime(validated_at) if validated_at else now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE records
                SET validation_state = ?,
                    validated_at = ?,
                    validated_by = ?,
                    validation_note = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (validation_state, timestamp, validated_by, note, now_iso(), record_id),
            )
            self.add_event(
                record_id,
                "validation_changed",
                validated_by,
                note=note,
                payload={
                    "validation_state": validation_state,
                    "validated_at": timestamp,
                    "validated_by": validated_by,
                    "validation_note": note,
                },
            )

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
        kind: str | None = None,
        status: str | None = None,
        exclude_status: list[str] | None = None,
        validation_state: str | None = None,
        include_obsolete: bool = False,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return [
            row
            for row in self.query_records(
                subject=subject,
                kind=kind,
                status=[status] if status else None,
                exclude_status=exclude_status,
                validation_state=validation_state,
                include_obsolete=include_obsolete,
                limit=limit,
                include_body=False,
                include_evidence=False,
                include_artifacts=False,
                as_rows=True,
            )
        ]

    def query_records(
        self,
        *,
        subject: str | None = None,
        kind: str | None = None,
        status: list[str] | None = None,
        exclude_status: list[str] | None = None,
        validation_state: str | None = None,
        tags: list[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_obsolete: bool = False,
        include_body: bool = False,
        include_evidence: bool = False,
        include_artifacts: bool = False,
        limit: int = 50,
        sort: str = "created_desc",
        as_rows: bool = False,
    ) -> list[dict[str, Any]] | list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject:
            clauses.append("(subject = ? OR subject LIKE ?)")
            params.extend([subject, f"{subject}.%"])
        if kind:
            validate_record_kind(kind)
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            for item in status:
                validate_record_status(item)
            placeholders = ",".join("?" for _ in status)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status)
        elif not include_obsolete:
            placeholders = ",".join("?" for _ in CURRENT_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(CURRENT_STATUSES)
        if exclude_status:
            for item in exclude_status:
                validate_record_status(item)
            placeholders = ",".join("?" for _ in exclude_status)
            clauses.append(f"status NOT IN ({placeholders})")
            params.extend(exclude_status)
        if validation_state:
            validate_validation_state(validation_state)
            clauses.append("validation_state = ?")
            params.append(validation_state)
        for tag in tags or []:
            clauses.append("EXISTS (SELECT 1 FROM record_tags rt WHERE rt.record_id = records.id AND rt.tag = ?)")
            params.append(tag)
        if created_from:
            clauses.append("created_at >= ?")
            params.append(parse_datetime(created_from))
        if created_to:
            clauses.append("created_at <= ?")
            params.append(parse_datetime(created_to))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = {
            "created_asc": "created_at ASC, id ASC",
            "created_desc": "created_at DESC, id DESC",
            "subject": "subject ASC, created_at DESC, id DESC",
        }.get(sort)
        if order_by is None:
            raise ValueError("sort must be created_desc, created_asc, or subject")
        params.append(limit)
        body_column = ", body" if include_body else ""
        rows = self.conn.execute(
            f"""
            SELECT id, subject, kind, status, validation_state, summary, created_at, created_by{body_column}
            FROM records
            {where}
            ORDER BY {order_by}
            LIMIT ?
            """,
            params,
        ).fetchall()
        if as_rows:
            return rows
        records = [dict(row) for row in rows]
        if include_evidence or include_artifacts:
            record_ids = [record["id"] for record in records]
            evidence_by_record: dict[str, list[dict[str, Any]]] = {record_id: [] for record_id in record_ids}
            artifact_by_record: dict[str, list[dict[str, Any]]] = {record_id: [] for record_id in record_ids}
            if include_evidence:
                for evidence in self.evidence_for_records(record_ids):
                    evidence_by_record[evidence["record_id"]].append(dict(evidence))
            if include_artifacts:
                for artifact in self.artifacts_for_records(record_ids):
                    artifact_by_record[artifact["record_id"]].append(dict(artifact))
            for record in records:
                if include_evidence:
                    record["evidence"] = evidence_by_record[record["id"]]
                if include_artifacts:
                    record["artifacts"] = artifact_by_record[record["id"]]
        return records

    def list_topics(
        self,
        *,
        subject: str | None = None,
        include_obsolete: bool = False,
        direct_only: bool = False,
    ) -> list[dict[str, Any]]:
        all_subjects = self._all_visible_subjects(include_obsolete=include_obsolete)
        prefixes: set[str] = set()
        for item in all_subjects:
            parts = item.split(".")
            for index in range(1, len(parts) + 1):
                prefix = ".".join(parts[:index])
                if subject and not (prefix == subject or prefix.startswith(subject + ".")):
                    continue
                if direct_only and subject and prefix != subject and len(prefix.split(".")) != len(subject.split(".")) + 1:
                    continue
                if direct_only and not subject and len(prefix.split(".")) != 1:
                    continue
                prefixes.add(prefix)

        topics = []
        for prefix in sorted(prefixes):
            direct_counts = self._topic_counts(prefix, include_obsolete=include_obsolete, subtree=False)
            subtree_counts = self._topic_counts(prefix, include_obsolete=include_obsolete, subtree=True)
            child_count = len(
                [
                    candidate
                    for candidate in prefixes
                    if candidate.startswith(prefix + ".")
                    and len(candidate.split(".")) == len(prefix.split(".")) + 1
                ]
            )
            topics.append(
                {
                    "subject": prefix,
                    "parent": ".".join(prefix.split(".")[:-1]) or None,
                    "depth": len(prefix.split(".")),
                    "direct_records": direct_counts["total"],
                    "direct_current": direct_counts["current"],
                    "direct_obsolete": direct_counts["obsolete"],
                    "subtree_records": subtree_counts["total"],
                    "subtree_current": subtree_counts["current"],
                    "subtree_obsolete": subtree_counts["obsolete"],
                    "child_topics": child_count,
                }
            )
        return topics

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        include_obsolete: bool = False,
        validation_state: str | None = None,
    ) -> list[sqlite3.Row]:
        status_clause = "" if include_obsolete else self._current_status_clause("r.status")
        params: list[Any] = [query]
        if not include_obsolete:
            params.extend(CURRENT_STATUSES)
        validation_clause = ""
        if validation_state:
            validate_validation_state(validation_state)
            validation_clause = "AND r.validation_state = ?"
            params.append(validation_state)
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT r.id, r.subject, r.kind, r.status, r.validation_state, r.summary, r.created_at,
                   bm25(records_fts) AS rank
            FROM records_fts
            JOIN records r ON r.rowid = records_fts.rowid
            WHERE records_fts MATCH ?
            {status_clause}
            {validation_clause}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()

    def vector_search(
        self,
        query: str,
        *,
        limit: int = 20,
        include_obsolete: bool = False,
        validation_state: str | None = None,
        embedder: Embedder | None = None,
        fail_soft: bool = True,
    ) -> dict[str, Any]:
        if fail_soft and embedder is None:
            return vector_search_records_if_available(
                self.conn,
                query,
                limit=limit,
                include_obsolete=include_obsolete,
                validation_state=validation_state,
            )
        return vector_search_records(
            self.conn,
            query,
            limit=limit,
            include_obsolete=include_obsolete,
            validation_state=validation_state,
            embedder=embedder,
        )

    def hybrid_search(
        self,
        query: str,
        *,
        limit: int = 20,
        include_obsolete: bool = False,
        validation_state: str | None = None,
        embedder: Embedder | None = None,
    ) -> dict[str, Any]:
        lexical_rows = [
            dict(row)
            for row in self.search(
                query,
                limit=limit,
                include_obsolete=include_obsolete,
                validation_state=validation_state,
            )
        ]
        vector_result = self.vector_search(
            query,
            limit=limit,
            include_obsolete=include_obsolete,
            validation_state=validation_state,
            embedder=embedder,
            fail_soft=embedder is None,
        )
        combined = fuse_search_results(lexical_rows, vector_result.get("results") or [], limit=limit)
        return {
            "query": query,
            "combined": combined,
            "lexical": {
                "available": True,
                "results": lexical_rows,
                "returned_count": len(lexical_rows),
            },
            "vector": vector_result,
        }

    def rebuild_vectors(
        self,
        *,
        embedder: Embedder | None = None,
        fail_soft: bool = True,
    ) -> dict[str, Any]:
        if fail_soft and embedder is None:
            return rebuild_record_vectors_if_available(self.conn)
        return rebuild_record_vectors(self.conn, embedder=embedder)

    def gather(self, subject: str, *, include_obsolete: bool = False) -> dict[str, Any]:
        namespace_rows = self.list_records(
            subject=subject,
            include_obsolete=include_obsolete,
            limit=500,
        )
        record_ids = [row["id"] for row in namespace_rows]
        associated_rows = self.associated_records(record_ids, include_obsolete=include_obsolete)
        evidence_rows = self.evidence_for_records(record_ids)
        artifact_rows = self.artifacts_for_records(record_ids)
        grouped = {
            "current": [dict(row) for row in namespace_rows if row["status"] in CURRENT_STATUSES],
            "obsolete": [dict(row) for row in namespace_rows if row["status"] in OBSOLETE_STATUSES],
            "associated": [dict(row) for row in associated_rows],
            "evidence": [dict(row) for row in evidence_rows],
            "artifacts": [dict(row) for row in artifact_rows],
        }
        return grouped

    def subject_view(
        self,
        subject: str,
        *,
        include_obsolete: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        rows = self.list_records(
            subject=subject,
            include_obsolete=include_obsolete,
            limit=limit,
        )
        entries: list[dict[str, Any]] = []
        for row in rows:
            record = self.get_record(row["id"])
            if not record:
                continue
            entries.append(
                {
                    "entry_type": "record",
                    "created_at": record["created_at"],
                    "record_id": record["id"],
                    "subject": record["subject"],
                    "kind": record["kind"],
                    "status": record["status"],
                    "validation_state": record["validation_state"],
                    "summary": record.get("summary"),
                    "body": record["body"],
                }
            )
            for artifact in record.get("artifacts", []):
                entries.append(
                    {
                        "entry_type": "artifact",
                        "created_at": artifact["created_at"],
                        "record_id": artifact["record_id"],
                        "artifact_id": artifact["id"],
                        "subject": artifact["subject"],
                        "artifact_type": artifact["type"],
                        "content_type": artifact["content_type"],
                        "label": artifact.get("label"),
                        "summary": artifact.get("summary"),
                        "url": f"/artifacts/{artifact['id']}/content",
                    }
                )
        entries.sort(key=lambda item: (item["created_at"], item.get("artifact_id") or item.get("record_id") or ""), reverse=True)
        return {
            "subject": subject,
            "include_obsolete": include_obsolete,
            "entries": entries,
        }

    def list_saved_views(
        self,
        *,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject:
            clauses.append("(subject = ? OR subject LIKE ?)")
            params.extend([subject, f"{subject}.%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT id, subject, title, query_json, created_at, created_by, export_visibility
            FROM saved_views
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [saved_view_from_row(row) for row in rows]

    def get_saved_view(self, view_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, subject, title, query_json, created_at, created_by, export_visibility
            FROM saved_views
            WHERE id = ?
            """,
            (view_id,),
        ).fetchone()
        return saved_view_from_row(row) if row else None

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
            "artifacts": [dict(item) for item in self.artifacts_for_records([record_id])],
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

    def artifacts_for_records(self, record_ids: list[str]) -> list[sqlite3.Row]:
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        return self.conn.execute(
            f"""
            SELECT id, record_id, subject, type, content_type, storage_path, label, summary,
                   source_uri, sha256, size_bytes, created_at, created_by, export_visibility
            FROM artifacts
            WHERE record_id IN ({placeholders})
            ORDER BY created_at, id
            """,
            record_ids,
        ).fetchall()

    def list_artifacts(
        self,
        *,
        subject: str | None = None,
        artifact_type: str | None = None,
        include_obsolete: bool = False,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject:
            clauses.append("(a.subject = ? OR a.subject LIKE ?)")
            params.extend([subject, f"{subject}.%"])
        if artifact_type:
            clauses.append("a.type = ?")
            params.append(artifact_type)
        if not include_obsolete:
            placeholders = ",".join("?" for _ in CURRENT_STATUSES)
            clauses.append(f"r.status IN ({placeholders})")
            params.extend(CURRENT_STATUSES)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT a.id, a.record_id, a.subject, a.type, a.content_type, a.storage_path,
                   a.label, a.summary, a.source_uri, a.sha256, a.size_bytes,
                   a.created_at, a.created_by, a.export_visibility,
                   r.status AS record_status, r.kind AS record_kind
            FROM artifacts a
            JOIN records r ON r.id = a.record_id
            {where}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT a.id, a.record_id, a.subject, a.type, a.content_type, a.storage_path,
                   a.label, a.summary, a.source_uri, a.sha256, a.size_bytes,
                   a.created_at, a.created_by, a.export_visibility,
                   r.status AS record_status, r.kind AS record_kind, r.summary AS record_summary
            FROM artifacts a
            JOIN records r ON r.id = a.record_id
            WHERE a.id = ?
            """,
            (artifact_id,),
        ).fetchone()
        return dict(row) if row else None

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
            SELECT DISTINCT r.id, r.subject, r.kind, r.status, r.validation_state, r.summary,
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

    def _all_visible_subjects(self, *, include_obsolete: bool) -> set[str]:
        status_clause = "" if include_obsolete else self._current_status_clause("status").removeprefix("AND ")
        params: list[Any] = []
        if not include_obsolete:
            params.extend(CURRENT_STATUSES)
        where = f"WHERE {status_clause}" if status_clause else ""
        primary = {
            row["subject"]
            for row in self.conn.execute(
                f"SELECT DISTINCT subject FROM records {where}",
                params,
            )
        }
        related = {
            row["subject"]
            for row in self.conn.execute(
                f"""
                SELECT DISTINCT rs.subject
                FROM record_subjects rs
                JOIN records r ON r.id = rs.record_id
                {where.replace('status', 'r.status')}
                """,
                params,
            )
        }
        return primary | related

    def _topic_counts(self, subject: str, *, include_obsolete: bool, subtree: bool) -> dict[str, int]:
        clauses = ["(subject = ?"]
        params: list[Any] = [subject]
        if subtree:
            clauses[0] += " OR subject LIKE ?"
            params.append(f"{subject}.%")
        clauses[0] += ")"
        if not include_obsolete:
            placeholders = ",".join("?" for _ in CURRENT_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(CURRENT_STATUSES)
        where = " AND ".join(clauses)
        row = self.conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status IN ('active', 'proposed', 'accepted', 'resolved') THEN 1 ELSE 0 END) AS current,
              SUM(CASE WHEN status IN ('superseded', 'rejected', 'withdrawn', 'archived') THEN 1 ELSE 0 END) AS obsolete
            FROM records
            WHERE {where}
            """,
            params,
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "current": int(row["current"] or 0),
            "obsolete": int(row["obsolete"] or 0),
        }


def fuse_search_results(
    lexical_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(lexical_rows, start=1):
        item = fused.setdefault(
            row["id"],
            {
                "id": row["id"],
                "subject": row["subject"],
                "kind": row["kind"],
                "status": row["status"],
                "validation_state": row["validation_state"],
                "summary": row.get("summary"),
                "created_at": row.get("created_at"),
                "sources": [],
                "score": 0.0,
            },
        )
        item["sources"].append("lexical")
        item["lexical_rank"] = index
        item["lexical_bm25"] = row.get("rank")
        item["score"] += 1.0 / (60.0 + index)
    for index, row in enumerate(vector_rows, start=1):
        item = fused.setdefault(
            row["id"],
            {
                "id": row["id"],
                "subject": row["subject"],
                "kind": row["kind"],
                "status": row["status"],
                "validation_state": row["validation_state"],
                "summary": row.get("summary"),
                "created_at": row.get("created_at"),
                "sources": [],
                "score": 0.0,
            },
        )
        item["sources"].append("vector")
        item["vector_rank"] = index
        item["vector_similarity"] = row.get("similarity")
        item["vector_distance"] = row.get("distance")
        item["score"] += 1.0 / (60.0 + index)
    results = sorted(
        fused.values(),
        key=lambda item: (-float(item["score"]), item.get("lexical_rank", 9999), item.get("vector_rank", 9999)),
    )
    for item in results:
        item["score"] = round(float(item["score"]), 6)
    return results[:limit]


def validate_validation_state(validation_state: str) -> None:
    if validation_state not in VALIDATION_STATES:
        allowed = ", ".join(VALIDATION_STATES)
        raise ValueError(f"unknown validation state: {validation_state}; expected one of {allowed}")


def validate_record_kind(kind: str) -> None:
    if kind not in RECORD_KINDS:
        allowed = ", ".join(RECORD_KINDS)
        raise ValueError(f"unknown record kind: {kind}; expected one of {allowed}")


def validate_record_status(status: str) -> None:
    if status not in RECORD_STATUSES:
        allowed = ", ".join(RECORD_STATUSES)
        raise ValueError(f"unknown record status: {status}; expected one of {allowed}")


def saved_view_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["query"] = json.loads(item.pop("query_json"))
    item["url"] = f"/saved-views/{item['id']}.html"
    return item
