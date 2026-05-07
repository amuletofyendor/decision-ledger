from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from decision_ledger.cli import main


def run_cli(db_path: Path, *args: str) -> int:
    return main(["--db", str(db_path), *args])


def test_add_show_and_search(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    assert run_cli(
        db_path,
        "add",
        "connected-ai.auth.oidc.client-persistence",
        "--kind",
        "decision",
        "--status",
        "accepted",
        "--validation-state",
        "partially_validated",
        "--summary",
        "Persist dynamic clients",
        "--body",
        "OpenIddict dynamic clients must survive identity restarts.",
        "--tag",
        "oidc",
        "--json",
    ) == 0
    record_id = json.loads(capsys.readouterr().out)["id"]
    event_file = tmp_path / "events" / "connected-ai" / "auth" / "oidc" / "client-persistence.jsonl"
    assert event_file.exists()
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "created"
    assert event["record_id"] == record_id
    assert event["subject"] == "connected-ai.auth.oidc.client-persistence"

    assert run_cli(db_path, "show", record_id, "--json") == 0
    record = json.loads(capsys.readouterr().out)
    assert record["subject"] == "connected-ai.auth.oidc.client-persistence"
    assert record["validation_state"] == "partially_validated"
    assert record["tags"] == ["oidc"]

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

    assert run_cli(db_path, "search", "OpenIddict", "--json") == 0
    results = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in results] == [record_id]

    assert run_cli(db_path, "list", "connected-ai.auth", "--validation-state", "validated", "--json") == 0
    validated_results = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in validated_results] == [record_id]

    db_path.unlink()
    assert run_cli(db_path, "show", record_id, "--json") == 0
    auto_rebuilt_record = json.loads(capsys.readouterr().out)
    assert auto_rebuilt_record["summary"] == "Persist dynamic clients"

    db_path.unlink()
    assert run_cli(db_path, "rebuild", "--json") == 0
    rebuild = json.loads(capsys.readouterr().out)
    assert rebuild["db_path"] == str(db_path)

    assert run_cli(db_path, "show", record_id, "--json") == 0
    rebuilt_record = json.loads(capsys.readouterr().out)
    assert rebuilt_record["summary"] == "Persist dynamic clients"
    assert rebuilt_record["validation_state"] == "validated"


def test_evidence_association_and_gather(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "First thought", "--summary", "First", "--json")
    first_id = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "connected-ai.environments.dev-aks.identity", "--body", "Environment fact", "--summary", "Env", "--json")
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

    assert run_cli(db_path, "gather", "connected-ai.auth", "--json") == 0
    gathered = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in gathered["current"]] == [first_id]
    assert [row["id"] for row in gathered["associated"]] == [second_id]
    assert gathered["evidence"][0]["uri"] == "/tmp/source.cs"

    event_text = (tmp_path / "events" / "connected-ai" / "auth" / "oidc.jsonl").read_text(encoding="utf-8")
    assert '"event_type":"evidence_added"' in event_text
    assert '"event_type":"associated"' in event_text


def test_topics_lists_subject_tree_counts(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "OIDC thought", "--json")
    capsys.readouterr()
    run_cli(db_path, "add", "connected-ai.auth.mcp", "--body", "MCP thought", "--json")
    capsys.readouterr()
    run_cli(
        db_path,
        "add",
        "connected-ai.retrieval.wiki",
        "--body",
        "Wiki thought",
        "--related-subject",
        "connected-ai.auth.related-source",
        "--json",
    )
    capsys.readouterr()

    assert run_cli(db_path, "topics", "connected-ai.auth", "--direct", "--json") == 0
    topics = json.loads(capsys.readouterr().out)
    subjects = [topic["subject"] for topic in topics]
    assert subjects == [
        "connected-ai.auth",
        "connected-ai.auth.mcp",
        "connected-ai.auth.oidc",
        "connected-ai.auth.related-source",
    ]
    root = topics[0]
    assert root["direct_records"] == 0
    assert root["subtree_records"] == 2
    assert root["child_topics"] == 3


def test_supersede_record(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "Old thought", "--summary", "Old", "--json")
    old_id = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "New thought", "--summary", "New", "--json")
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

    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "Old A", "--json")
    old_a = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "connected-ai.auth.oidc.client-persistence", "--body", "Old B", "--json")
    old_b = json.loads(capsys.readouterr().out)["id"]
    run_cli(db_path, "add", "connected-ai.auth.oidc", "--body", "Replacement", "--json")
    replacement = json.loads(capsys.readouterr().out)["id"]

    assert run_cli(
        db_path,
        "supersede",
        "connected-ai.auth.oidc",
        "--before",
        "2999-01-01 00:00",
        "--replacement",
        replacement,
        "--json",
    ) == 0
    superseded = json.loads(capsys.readouterr().out)["superseded"]
    assert superseded == [old_a, old_b]


def test_wiki_export_generates_static_subject_tree(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"
    out_dir = tmp_path / "public"

    run_cli(
        db_path,
        "add",
        "connected-ai.auth.oidc.client-persistence",
        "--body",
        "Dynamic clients must survive restarts.",
        "--summary",
        "Persist dynamic clients",
        "--visibility",
        "internal",
        "--tag",
        "oidc",
        "--json",
    )
    record_id = json.loads(capsys.readouterr().out)["id"]
    run_cli(
        db_path,
        "evidence",
        "add",
        record_id,
        "--type",
        "url",
        "--uri",
        "https://example.test/evidence",
        "--visibility",
        "internal",
        "--json",
    )
    capsys.readouterr()

    assert run_cli(
        db_path,
        "wiki",
        "connected-ai.auth",
        "--out",
        str(out_dir),
        "--json",
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["records"] == 1
    assert result["subject_pages"] == 3
    assert (out_dir / "index.html").exists()
    assert (out_dir / "subjects" / "connected-ai" / "auth" / "index.html").exists()
    assert (out_dir / "records" / record_id / "index.html").exists()
    assert (out_dir / "assets" / "search-index.json").exists()
    assert (out_dir / "assets" / "graph.json").exists()

    record_html = (out_dir / "records" / record_id / "index.html").read_text(encoding="utf-8")
    assert "Persist dynamic clients" in record_html
    assert "unvalidated" in record_html
    assert "https://example.test/evidence" in record_html
    assert "../../assets/styles.css" in record_html

    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert '<ul class="tree">' in index_html
    assert "connected-ai.auth.oidc.client-persistence" in index_html


def test_wiki_export_profile_filters_private_records(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"

    run_cli(
        db_path,
        "add",
        "connected-ai.auth.private",
        "--body",
        "Private thought",
        "--visibility",
        "private",
        "--json",
    )
    capsys.readouterr()
    run_cli(
        db_path,
        "add",
        "connected-ai.auth.public",
        "--body",
        "Public thought",
        "--visibility",
        "public",
        "--json",
    )
    capsys.readouterr()

    assert run_cli(
        db_path,
        "wiki",
        "connected-ai.auth",
        "--out",
        str(tmp_path / "public-export"),
        "--profile",
        "public",
        "--json",
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["records"] == 1

    search_index = json.loads((tmp_path / "public-export" / "assets" / "search-index.json").read_text(encoding="utf-8"))
    assert search_index[0]["subject"] == "connected-ai.auth.public"
    assert search_index[0]["validation_state"] == "unvalidated"
