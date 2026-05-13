from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from decision_ledger.cli import main
from decision_ledger.db import connect
from decision_ledger.event_store import EventStore, EventedLedger
from decision_ledger.vector_search import bounded_embedding_text


def run_cli(db_path: Path, *args: str) -> int:
    return main(["--db", str(db_path), *args])


def test_add_show_and_search(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    assert run_cli(
        db_path,
        "add",
        "product.auth.oauth.client-persistence",
        "--kind",
        "decision",
        "--status",
        "accepted",
        "--validation-state",
        "partially_validated",
        "--summary",
        "Persist registered clients",
        "--body",
        "OAuth registered clients must survive auth service restarts.",
        "--tag",
        "oauth",
        "--json",
    ) == 0
    record_id = json.loads(capsys.readouterr().out)["id"]
    event_file = tmp_path / "events" / "product" / "auth" / "oauth" / "client-persistence.jsonl"
    assert event_file.exists()
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "created"
    assert event["record_id"] == record_id
    assert event["subject"] == "product.auth.oauth.client-persistence"

    assert run_cli(db_path, "show", record_id, "--json") == 0
    record = json.loads(capsys.readouterr().out)
    assert record["subject"] == "product.auth.oauth.client-persistence"
    assert record["validation_state"] == "partially_validated"
    assert record["tags"] == ["oauth"]

    assert run_cli(
        db_path,
        "validate",
        record_id,
        "--state",
        "validated",
        "--validated-by",
        "pytest",
        "--note",
        "Confirmed in test",
        "--json",
    ) == 0
    validation_result = json.loads(capsys.readouterr().out)
    assert validation_result["validation_state"] == "validated"

    assert run_cli(db_path, "show", record_id, "--json") == 0
    validated_record = json.loads(capsys.readouterr().out)
    assert validated_record["validation_state"] == "validated"
    assert validated_record["validated_by"] == "pytest"
    assert validated_record["validation_note"] == "Confirmed in test"
    assert [event["event_type"] for event in validated_record["events"]] == ["created", "validation_changed"]

    assert run_cli(db_path, "search", "OAuth", "--json") == 0
    results = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in results] == [record_id]

    assert run_cli(db_path, "list", "product.auth", "--validation-state", "validated", "--json") == 0
    validated_results = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in validated_results] == [record_id]

    db_path.unlink()
    assert run_cli(db_path, "show", record_id, "--json") == 0
    auto_rebuilt_record = json.loads(capsys.readouterr().out)
    assert auto_rebuilt_record["summary"] == "Persist registered clients"

    db_path.unlink()
    assert run_cli(db_path, "rebuild", "--json") == 0
    rebuild = json.loads(capsys.readouterr().out)
    assert rebuild["db_path"] == str(db_path)

    assert run_cli(db_path, "show", record_id, "--json") == 0
    rebuilt_record = json.loads(capsys.readouterr().out)
    assert rebuilt_record["summary"] == "Persist registered clients"
    assert rebuilt_record["validation_state"] == "validated"


class FakeEmbedder:
    provider = "fake"
    model = "fake-3d"
    dimensions = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        lowered = text.lower()
        if "vector" in lowered or "semantic" in lowered:
            return [0.0, 1.0, 0.0]
        if "supplier" in lowered:
            return [0.0, 0.0, 1.0]
        return [1.0, 0.0, 0.0]


def test_vector_search_uses_generated_projection(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    conn = connect(db_path)
    ledger = EventedLedger(conn, EventStore(tmp_path), db_path)
    lexical_only = ledger.add_record(
        subject="decision-ledger.search.lexical",
        kind="decision",
        status="accepted",
        summary="Keep FTS",
        body="Use exact full text search for precise lookup.",
    )
    vector_record = ledger.add_record(
        subject="decision-ledger.search.vector",
        kind="decision",
        status="accepted",
        summary="Add semantic lookup",
        body="Use generated vector embeddings for semantic recall.",
        tags=["retrieval"],
        related_subjects=["decision-ledger.search"],
    )

    result = ledger.vector_search(
        "semantic memory",
        limit=2,
        embedder=FakeEmbedder(),
        fail_soft=False,
    )

    assert result["model"] == "fake-3d"
    assert result["dimensions"] == 3
    assert [row["id"] for row in result["results"]] == [vector_record, lexical_only]

    event_files = list((tmp_path / "events").rglob("*.jsonl"))
    assert event_files
    assert "record_embedding_metadata" not in "\n".join(
        path.read_text(encoding="utf-8") for path in event_files
    )

    hybrid = ledger.hybrid_search(
        "semantic",
        limit=2,
        embedder=FakeEmbedder(),
    )
    assert hybrid["combined"][0]["id"] == vector_record
    assert hybrid["combined"][0]["sources"] == ["lexical", "vector"]
    assert hybrid["lexical"]["available"] is True
    assert hybrid["vector"]["model"] == "fake-3d"


def test_embedding_text_is_bounded_without_changing_canonical_body(monkeypatch) -> None:
    monkeypatch.setenv("DECISION_LEDGER_VECTOR_MAX_TEXT_CHARS", "220")
    text = "body: " + ("important detail " * 20)

    bounded = bounded_embedding_text(text)

    assert len(bounded) == 220
    assert bounded.startswith("body: important")
    assert "embedding_text_truncated" in bounded
    assert "original_sha256=" in bounded


def test_evidence_association_and_gather(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "product.auth.oauth", "--body", "First thought", "--summary", "First", "--json")
    first_id = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "product.environments.staging.auth", "--body", "Environment fact", "--summary", "Env", "--json")
    second_id = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(
        db_path,
        "evidence",
        "add",
        first_id,
        "--type",
        "file",
        "--uri",
        "/tmp/source.cs",
        "--line",
        "42",
        "--json",
    ) == 0
    assert json.loads(capsys.readouterr().out)["id"].startswith("evd_")

    assert run_cli(
        db_path,
        "associate",
        first_id,
        second_id,
        "--relation",
        "depends_on",
        "--note",
        "Auth depends on shared DB ownership",
        "--json",
    ) == 0
    assert json.loads(capsys.readouterr().out)["id"].startswith("asc_")

    assert run_cli(db_path, "gather", "product.auth", "--json") == 0
    gathered = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in gathered["current"]] == [first_id]
    assert [row["id"] for row in gathered["associated"]] == [second_id]
    assert gathered["evidence"][0]["uri"] == "/tmp/source.cs"

    event_text = (tmp_path / "events" / "product" / "auth" / "oauth.jsonl").read_text(encoding="utf-8")
    assert '"event_type":"evidence_added"' in event_text
    assert '"event_type":"associated"' in event_text


def test_html_and_image_artifacts_are_stored_and_rebuilt(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"
    html_path = tmp_path / "demo.html"
    image_path = tmp_path / "diagram.png"
    html_path.write_text(
        "<!doctype html><style>body{color:#123}</style><script>window.demo=true</script><p>Artifact demo</p>",
        encoding="utf-8",
    )
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nexample")

    assert run_cli(
        db_path,
        "artifact",
        "add-html",
        "product.demos.sample",
        "--file",
        str(html_path),
        "--summary",
        "Demo HTML",
        "--visibility",
        "internal",
        "--json",
    ) == 0
    html_result = json.loads(capsys.readouterr().out)
    assert html_result["type"] == "html"
    assert html_result["url"] == f"/artifacts/{html_result['id']}/content"
    assert (tmp_path / html_result["storage_path"]).read_text(encoding="utf-8").startswith("<!doctype html>")

    assert run_cli(
        db_path,
        "artifact",
        "add-image",
        "product.demos.sample",
        "--file",
        str(image_path),
        "--summary",
        "Demo image",
        "--visibility",
        "internal",
        "--json",
    ) == 0
    image_result = json.loads(capsys.readouterr().out)
    assert image_result["type"] == "image"
    assert image_result["content_type"] == "image/png"
    assert (tmp_path / image_result["storage_path"]).read_bytes().startswith(b"\x89PNG")

    assert run_cli(db_path, "artifact", "list", "product.demos", "--json") == 0
    artifacts = json.loads(capsys.readouterr().out)
    assert {artifact["id"] for artifact in artifacts} == {html_result["id"], image_result["id"]}

    assert run_cli(db_path, "view", "product.demos", "--json") == 0
    view = json.loads(capsys.readouterr().out)
    assert {entry["entry_type"] for entry in view["entries"]} == {"record", "artifact"}
    assert html_result["id"] in {entry.get("artifact_id") for entry in view["entries"]}

    assert run_cli(db_path, "show", html_result["record_id"], "--json") == 0
    record = json.loads(capsys.readouterr().out)
    assert record["artifacts"][0]["id"] == html_result["id"]

    event_text = (tmp_path / "events" / "product" / "demos" / "sample.jsonl").read_text(encoding="utf-8")
    assert '"event_type":"artifact_added"' in event_text

    db_path.unlink()
    assert run_cli(db_path, "rebuild", "--skip-vectors", "--json") == 0
    capsys.readouterr()
    assert run_cli(db_path, "artifact", "list", "product.demos", "--json") == 0
    rebuilt = json.loads(capsys.readouterr().out)
    assert {artifact["id"] for artifact in rebuilt} == {html_result["id"], image_result["id"]}


def test_cli_accepts_idea_kind(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    assert run_cli(
        db_path,
        "add",
        "product.auth.oauth.client-persistence",
        "--kind",
        "idea",
        "--summary",
        "Try preserving clients",
        "--body",
        "Idea: preserve registered clients across auth service restarts.",
        "--json",
    ) == 0
    record_id = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(db_path, "show", record_id, "--json") == 0
    record = json.loads(capsys.readouterr().out)
    assert record["kind"] == "idea"


def test_cli_accepts_snag_kind(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    assert run_cli(
        db_path,
        "add",
        "product.auth.oauth.client-persistence",
        "--kind",
        "snag",
        "--summary",
        "Client cleanup snag",
        "--body",
        "Snag: migrate old dynamic-client cleanup notes into the ledger.",
        "--json",
    ) == 0
    record_id = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(db_path, "show", record_id, "--json") == 0
    record = json.loads(capsys.readouterr().out)
    assert record["kind"] == "snag"


def test_list_filters_by_kind_and_excluded_status(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    assert run_cli(
        db_path,
        "add",
        "decision-ledger.snags.open",
        "--kind",
        "snag",
        "--summary",
        "Open snag",
        "--body",
        "Still open.",
        "--json",
    ) == 0
    open_id = json.loads(capsys.readouterr().out)["id"]
    assert run_cli(
        db_path,
        "add",
        "decision-ledger.snags.done",
        "--kind",
        "snag",
        "--status",
        "resolved",
        "--summary",
        "Resolved snag",
        "--body",
        "Already done.",
        "--json",
    ) == 0
    capsys.readouterr()

    assert run_cli(db_path, "list", "--kind", "snag", "--exclude-status", "resolved", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == [open_id]


def test_existing_db_kind_constraint_migrates_for_new_kinds(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE records (
          id TEXT PRIMARY KEY,
          subject TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (
            kind IN ('thought', 'decision', 'assumption', 'question', 'finding', 'plan', 'note')
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
        INSERT INTO records (
          rowid, id, subject, kind, status, validation_state, summary, body,
          created_at, updated_at, valid_from, export_visibility
        )
        VALUES (
          7, 'rec_old', 'decision-ledger.test', 'thought', 'active', 'unvalidated',
          'Old thought', 'Existing content survives migration.', '2026-05-07T10:00:00+01:00',
          '2026-05-07T10:00:00+01:00', '2026-05-07T10:00:00+01:00', 'private'
        );
        """
    )
    raw.close()

    conn = connect(db_path)
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'records'"
    ).fetchone()["sql"]
    assert "'idea'" in table_sql
    assert "'snag'" in table_sql
    conn.execute(
        """
        INSERT INTO records (
          id, subject, kind, status, validation_state, summary, body,
          created_at, updated_at, valid_from, export_visibility
        )
        VALUES (
          'rec_idea', 'decision-ledger.idea', 'idea', 'active', 'unvalidated',
          'New idea', 'A migrated ledger can store ideas.', '2026-05-07T11:00:00+01:00',
          '2026-05-07T11:00:00+01:00', '2026-05-07T11:00:00+01:00', 'private'
        )
        """
    )
    conn.commit()
    conn.execute(
        """
        INSERT INTO records (
          id, subject, kind, status, validation_state, summary, body,
          created_at, updated_at, valid_from, export_visibility
        )
        VALUES (
          'rec_snag', 'decision-ledger.snag', 'snag', 'active', 'unvalidated',
          'New snag', 'A migrated ledger can store snags.', '2026-05-07T11:05:00+01:00',
          '2026-05-07T11:05:00+01:00', '2026-05-07T11:05:00+01:00', 'private'
        )
        """
    )
    conn.commit()

    assert conn.execute("SELECT kind FROM records WHERE id = 'rec_old'").fetchone()["kind"] == "thought"
    assert conn.execute("SELECT kind FROM records WHERE id = 'rec_idea'").fetchone()["kind"] == "idea"
    assert conn.execute("SELECT kind FROM records WHERE id = 'rec_snag'").fetchone()["kind"] == "snag"
    assert conn.execute("SELECT rowid FROM records_fts WHERE records_fts MATCH 'survives'").fetchone()["rowid"] == 7


def test_topics_lists_subject_tree_counts(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "product.auth.oauth", "--body", "OAuth thought", "--json")
    capsys.readouterr()
    run_cli(db_path, "add", "product.auth.mcp", "--body", "MCP thought", "--json")
    capsys.readouterr()
    run_cli(
        db_path,
        "add",
        "product.retrieval.wiki",
        "--body",
        "Wiki thought",
        "--related-subject",
        "product.auth.related-source",
        "--json",
    )
    capsys.readouterr()

    assert run_cli(db_path, "topics", "product.auth", "--direct", "--json") == 0
    topics = json.loads(capsys.readouterr().out)
    subjects = [topic["subject"] for topic in topics]
    assert subjects == [
        "product.auth",
        "product.auth.mcp",
        "product.auth.oauth",
        "product.auth.related-source",
    ]
    root = topics[0]
    assert root["direct_records"] == 0
    assert root["subtree_records"] == 2
    assert root["child_topics"] == 3


def test_supersede_record(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "product.auth.oauth", "--body", "Old thought", "--summary", "Old", "--json")
    old_id = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "product.auth.oauth", "--body", "New thought", "--summary", "New", "--json")
    new_id = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(db_path, "supersede", old_id, new_id, "--note", "New replaces old", "--json") == 0
    assert json.loads(capsys.readouterr().out)["superseded"] == [old_id]

    assert run_cli(db_path, "show", old_id, "--json") == 0
    old_record = json.loads(capsys.readouterr().out)
    assert old_record["status"] == "superseded"

    conn = sqlite3.connect(db_path)
    relation = conn.execute(
        "SELECT relation FROM record_associations WHERE from_record_id = ? AND to_record_id = ?",
        (new_id, old_id),
    ).fetchone()[0]
    assert relation == "supersedes"


def test_bulk_supersede_subject_before(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "product.auth.oauth", "--body", "Old A", "--json")
    old_a = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "product.auth.oauth.client-persistence", "--body", "Old B", "--json")
    old_b = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "product.auth.oauth", "--body", "Replacement", "--json")
    replacement = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(
        db_path,
        "supersede",
        "product.auth.oauth",
        "--before",
        "2999-01-01 00:00",
        "--replacement",
        replacement,
        "--json",
    ) == 0
    superseded = json.loads(capsys.readouterr().out)["superseded"]
    assert superseded == [old_a, old_b]
