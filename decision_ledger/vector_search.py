from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .model import CURRENT_STATUSES, now_iso


DEFAULT_PROVIDER = "ollama"
DEFAULT_MODEL = "nomic-embed-text:latest"
DEFAULT_DIMENSIONS = 768
DEFAULT_TEXT_SCHEMA = "record_text_v1"
DEFAULT_BATCH_SIZE = 32
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MAX_TEXT_CHARS = 8000
VECTOR_TABLE = "record_embeddings_vec"
METADATA_TABLE = "record_embedding_metadata"


class VectorUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    provider: str
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class RecordEmbeddingInput:
    rowid: int
    record_id: str
    text: str
    content_hash: str


class OllamaEmbedder:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout_s: float = 30.0,
    ) -> None:
        self.provider = DEFAULT_PROVIDER
        self.model = model or os.getenv("DECISION_LEDGER_VECTOR_MODEL", DEFAULT_MODEL)
        self.dimensions = dimensions
        self.base_url = (base_url or os.getenv("DECISION_LEDGER_OLLAMA_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout_s = timeout_s

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise VectorUnavailable(
                f"Ollama embeddings unavailable at {self.base_url}: HTTP {exc.code}: {detail}"
            ) from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise VectorUnavailable(f"Ollama embeddings unavailable at {self.base_url}: {exc}") from exc

        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise VectorUnavailable("Ollama returned an invalid embeddings response")
        vectors = [list(map(float, embedding)) for embedding in embeddings]
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise VectorUnavailable(
                    f"embedding dimension mismatch: expected {self.dimensions}, got {len(vector)}"
                )
        return vectors


def rebuild_record_vectors(
    conn: sqlite3.Connection,
    *,
    embedder: Embedder | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    embedder = embedder or OllamaEmbedder(dimensions=vector_dimensions())
    batch_size = batch_size or int(os.getenv("DECISION_LEDGER_VECTOR_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    load_sqlite_vec(conn)
    reset_vector_schema(conn, embedder.dimensions)
    records = embedding_inputs(conn)
    embedded = embed_records(conn, records, embedder=embedder, batch_size=batch_size)
    return {
        "available": True,
        "provider": embedder.provider,
        "model": embedder.model,
        "dimensions": embedder.dimensions,
        "text_schema": DEFAULT_TEXT_SCHEMA,
        "embedded": embedded,
        "records": len(records),
    }


def rebuild_record_vectors_if_available(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        return rebuild_record_vectors(conn)
    except VectorUnavailable as exc:
        return {"available": False, "error": str(exc)}


def vector_search_records(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    include_obsolete: bool = False,
    validation_state: str | None = None,
    embedder: Embedder | None = None,
    build_missing: bool = True,
) -> dict[str, Any]:
    query_text = query.strip()
    if not query_text:
        return {"query": query, "results": [], "error": "empty_query"}

    embedder = embedder or OllamaEmbedder(dimensions=vector_dimensions())
    load_sqlite_vec(conn)
    ensure_vector_schema(conn, embedder.dimensions)
    if build_missing:
        records = stale_embedding_inputs(conn, embedder=embedder)
        embed_records(conn, records, embedder=embedder)

    query_vector = embedder.embed([query_text])[0]
    k = max(limit * 4, limit)
    vector_blob = serialize_vector(query_vector)

    filters: list[str] = []
    params: list[Any] = [vector_blob, k]
    if not include_obsolete:
        placeholders = ",".join("?" for _ in CURRENT_STATUSES)
        filters.append(f"r.status IN ({placeholders})")
        params.extend(CURRENT_STATUSES)
    if validation_state:
        filters.append("r.validation_state = ?")
        params.append(validation_state)
    filter_sql = f"AND {' AND '.join(filters)}" if filters else ""
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT r.id, r.subject, r.kind, r.status, r.validation_state, r.summary,
               r.created_at, v.distance, m.content_hash, m.embedded_at
        FROM {VECTOR_TABLE} v
        JOIN records r ON r.rowid = v.rowid
        JOIN {METADATA_TABLE} m ON m.record_id = r.id
        WHERE v.embedding MATCH ?
          AND k = ?
          {filter_sql}
        ORDER BY v.distance
        LIMIT ?
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        distance = float(row["distance"])
        similarity = max(-1.0, min(1.0, 1.0 - ((distance * distance) / 2.0)))
        results.append({
            "id": row["id"],
            "subject": row["subject"],
            "kind": row["kind"],
            "status": row["status"],
            "validation_state": row["validation_state"],
            "summary": row["summary"],
            "created_at": row["created_at"],
            "distance": round(distance, 6),
            "similarity": round(similarity, 6),
        })
    return {
        "query": query,
        "results": results,
        "requested_limit": limit,
        "returned_count": len(results),
        "provider": embedder.provider,
        "model": embedder.model,
        "dimensions": embedder.dimensions,
        "text_schema": DEFAULT_TEXT_SCHEMA,
    }


def vector_search_records_if_available(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    include_obsolete: bool = False,
    validation_state: str | None = None,
) -> dict[str, Any]:
    try:
        return vector_search_records(
            conn,
            query,
            limit=limit,
            include_obsolete=include_obsolete,
            validation_state=validation_state,
        )
    except VectorUnavailable as exc:
        return {"query": query, "results": [], "available": False, "error": str(exc)}


def stale_embedding_inputs(conn: sqlite3.Connection, *, embedder: Embedder) -> list[RecordEmbeddingInput]:
    records = embedding_inputs(conn)
    if not records:
        return []
    metadata = {
        row["record_id"]: dict(row)
        for row in conn.execute(
            f"""
            SELECT record_id, provider, model, dimensions, text_schema, content_hash
            FROM {METADATA_TABLE}
            """
        )
    }
    stale = []
    for record in records:
        existing = metadata.get(record.record_id)
        if (
            not existing
            or existing["provider"] != embedder.provider
            or existing["model"] != embedder.model
            or int(existing["dimensions"]) != embedder.dimensions
            or existing["text_schema"] != DEFAULT_TEXT_SCHEMA
            or existing["content_hash"] != record.content_hash
        ):
            stale.append(record)
    return stale


def embed_records(
    conn: sqlite3.Connection,
    records: list[RecordEmbeddingInput],
    *,
    embedder: Embedder,
    batch_size: int | None = None,
) -> int:
    if not records:
        return 0
    batch_size = batch_size or int(os.getenv("DECISION_LEDGER_VECTOR_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    embedded = 0
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        vectors = embedder.embed([record.text for record in batch])
        timestamp = now_iso()
        with conn:
            for record, vector in zip(batch, vectors, strict=True):
                conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE rowid = ?", (record.rowid,))
                conn.execute(
                    f"INSERT INTO {VECTOR_TABLE}(rowid, embedding) VALUES (?, ?)",
                    (record.rowid, serialize_vector(vector)),
                )
                conn.execute(
                    f"""
                    INSERT INTO {METADATA_TABLE} (
                      record_id, record_rowid, provider, model, dimensions,
                      text_schema, content_hash, embedded_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(record_id) DO UPDATE SET
                      record_rowid = excluded.record_rowid,
                      provider = excluded.provider,
                      model = excluded.model,
                      dimensions = excluded.dimensions,
                      text_schema = excluded.text_schema,
                      content_hash = excluded.content_hash,
                      embedded_at = excluded.embedded_at
                    """,
                    (
                        record.record_id,
                        record.rowid,
                        embedder.provider,
                        embedder.model,
                        embedder.dimensions,
                        DEFAULT_TEXT_SCHEMA,
                        record.content_hash,
                        timestamp,
                    ),
                )
        embedded += len(batch)
    return embedded


def embedding_inputs(conn: sqlite3.Connection) -> list[RecordEmbeddingInput]:
    rows = conn.execute(
        """
        SELECT rowid, id, subject, kind, status, validation_state, summary, body
        FROM records
        ORDER BY created_at, id
        """
    ).fetchall()
    inputs = []
    for row in rows:
        tags = [
            item["tag"]
            for item in conn.execute(
                "SELECT tag FROM record_tags WHERE record_id = ? ORDER BY tag",
                (row["id"],),
            )
        ]
        related_subjects = [
            f"{item['relation']}:{item['subject']}"
            for item in conn.execute(
                """
                SELECT subject, relation
                FROM record_subjects
                WHERE record_id = ?
                ORDER BY relation, subject
                """,
                (row["id"],),
            )
        ]
        text = bounded_embedding_text(record_text(row, tags=tags, related_subjects=related_subjects))
        inputs.append(
            RecordEmbeddingInput(
                rowid=int(row["rowid"]),
                record_id=row["id"],
                text=text,
                content_hash=content_hash(text),
            )
        )
    return inputs


def record_text(row: sqlite3.Row, *, tags: list[str], related_subjects: list[str]) -> str:
    parts = [
        f"subject: {row['subject']}",
        f"kind: {row['kind']}",
        f"status: {row['status']}",
        f"validation_state: {row['validation_state']}",
    ]
    if row["summary"]:
        parts.append(f"summary: {row['summary']}")
    parts.append(f"body: {row['body']}")
    if tags:
        parts.append("tags: " + ", ".join(tags))
    if related_subjects:
        parts.append("related_subjects: " + ", ".join(related_subjects))
    return "\n".join(parts)


def bounded_embedding_text(text: str) -> str:
    max_chars = int(os.getenv("DECISION_LEDGER_VECTOR_MAX_TEXT_CHARS", str(DEFAULT_MAX_TEXT_CHARS)))
    if len(text) <= max_chars:
        return text
    digest = content_hash(text)
    marker = f"\n\n[embedding_text_truncated original_sha256={digest} original_chars={len(text)}]"
    return text[: max(0, max_chars - len(marker))] + marker


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_dimensions() -> int:
    return int(os.getenv("DECISION_LEDGER_VECTOR_DIMENSIONS", str(DEFAULT_DIMENSIONS)))


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ModuleNotFoundError as exc:
        raise VectorUnavailable("sqlite-vec is not installed; install the decision-ledger vector dependency") from exc
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except sqlite3.OperationalError as exc:
        if "already exists" not in str(exc).lower():
            raise VectorUnavailable(f"sqlite-vec could not be loaded: {exc}") from exc


def serialize_vector(vector: list[float]) -> bytes:
    try:
        import sqlite_vec
    except ModuleNotFoundError as exc:
        raise VectorUnavailable("sqlite-vec is not installed; install the decision-ledger vector dependency") from exc
    return sqlite_vec.serialize_float32(vector)


def reset_vector_schema(conn: sqlite3.Connection, dimensions: int) -> None:
    with conn:
        conn.execute(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {METADATA_TABLE}")
        create_vector_schema(conn, dimensions)


def ensure_vector_schema(conn: sqlite3.Connection, dimensions: int) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?",
        (VECTOR_TABLE,),
    ).fetchone()
    if row and f"float[{dimensions}]" not in (row["sql"] or ""):
        reset_vector_schema(conn, dimensions)
        return
    create_vector_schema(conn, dimensions)


def create_vector_schema(conn: sqlite3.Connection, dimensions: int) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
          record_id TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
          record_rowid INTEGER NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          dimensions INTEGER NOT NULL,
          text_schema TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          embedded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_record_embedding_metadata_model
        ON {METADATA_TABLE}(provider, model, dimensions, text_schema)
        """
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0(embedding float[{dimensions}])"
    )


def timed_vector_rebuild(conn: sqlite3.Connection) -> dict[str, Any]:
    started = time.perf_counter()
    result = rebuild_record_vectors_if_available(conn)
    result["elapsed_s"] = round(time.perf_counter() - started, 3)
    return result
