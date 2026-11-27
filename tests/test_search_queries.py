from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from decision_ledger.db import connect
from decision_ledger.event_store import EventedLedger, EventStore
from decision_ledger.repository import Ledger, fts5_literal


class FakeEmbedder:
    provider = "fake"
    model = "fake-2d"
    dimensions = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] if "semantic" in text.lower() or "vector" in text.lower() else [1.0, 0.0] for text in texts]


def make_ledger(tmp_path: Path) -> EventedLedger:
    db_path = tmp_path / "ledger.sqlite"
    return EventedLedger(connect(db_path), EventStore(tmp_path), db_path)


def add(ledger: EventedLedger, subject: str, body: str) -> str:
    return ledger.add_record(subject=subject, kind="thought", status="active", summary=None, body=body)


def test_fts5_literal_quotes_each_token() -> None:
    assert fts5_literal("sqlite-vec") == '"sqlite-vec"'
    assert fts5_literal('  say "hi  there ') == '"say" """hi" "there"'
    assert fts5_literal("   ") == ""


@pytest.mark.parametrize("query", ["sqlite-vec", "agent-hub", "vector:search"])
def test_punctuated_queries_find_the_literal_text(tmp_path: Path, query: str) -> None:
    ledger = make_ledger(tmp_path)
    record_id = add(ledger, "decision-ledger.search.punctuation", f"The {query} entry is here.")
    add(ledger, "decision-ledger.search.other", "Nothing relevant.")

    rows = ledger.search(query)

    assert [row["id"] for row in rows] == [record_id]


@pytest.mark.parametrize("query", ["C#", "it's", 'say "hi', "(unbalanced", "NEAR("])
def test_queries_with_fts5_syntax_errors_do_not_raise(tmp_path: Path, query: str) -> None:
    ledger = make_ledger(tmp_path)
    add(ledger, "decision-ledger.search.syntax", "C# and it's say hi unbalanced near")

    ledger.search(query)


def test_fts5_operators_still_work(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    only_hub = add(ledger, "decision-ledger.search.or", "The hub coordinates turns.")

    rows = ledger.search("ledger OR hub")

    assert [row["id"] for row in rows] == [only_hub]


def test_search_details_report_literal_mode(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    add(ledger, "decision-ledger.search.mode", "sqlite-vec loads")

    assert ledger.search_with_mode("sqlite-vec")[1] == "literal"
    assert ledger.search_with_mode("loads")[1] == "fts5"


def test_hybrid_search_handles_hyphenated_query(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    record_id = add(ledger, "decision-ledger.search.hybrid", "sqlite-vec is the vector extension.")

    result = ledger.hybrid_search("sqlite-vec", limit=5, embedder=FakeEmbedder())

    assert record_id in [row["id"] for row in result["lexical"]["results"]]
    assert result["lexical"]["query_mode"] == "literal"


def test_hybrid_search_falls_back_to_vectors_when_lexical_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = make_ledger(tmp_path)
    add(ledger, "decision-ledger.search.vector-only", "Semantic recall works.")

    def broken(self: Ledger, *args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(Ledger, "search_with_mode", broken)

    result = ledger.hybrid_search("semantic", limit=5, embedder=FakeEmbedder())

    assert result["mode"] == "vector_only"
    assert [row["id"] for row in result["combined"]]
    assert result["lexical"]["available"] is False
    assert "malformed" in result["warning"]


def test_other_operational_errors_still_raise_from_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = make_ledger(tmp_path)
    ledger.conn.execute("DROP TABLE records_fts")

    with pytest.raises(sqlite3.OperationalError):
        ledger.search("anything")
