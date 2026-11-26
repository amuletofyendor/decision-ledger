from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from decision_ledger import vector_search
from decision_ledger.cli import main
from decision_ledger.mcp_server import MCPServer
from decision_ledger.vector_search import VectorUnavailable

SUBJECT = "decision-ledger.safety.vectors"


@pytest.fixture
def no_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_conn: sqlite3.Connection) -> None:
        raise VectorUnavailable("sqlite-vec is not installed")

    monkeypatch.setattr(vector_search, "load_sqlite_vec", unavailable)


def tool(server: MCPServer, name: str, arguments: dict) -> dict:
    response = server.handle_wire_message(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    )[0]
    return response["result"]["structuredContent"]


def test_cli_rebuild_reports_an_unavailable_vector_index(tmp_path: Path, capsys, no_vectors: None) -> None:
    db_path = tmp_path / "ledger.sqlite"
    assert main(["--db", str(db_path), "add", SUBJECT, "--body", "x"]) == 0
    capsys.readouterr()

    assert main(["--db", str(db_path), "rebuild"]) == 0

    captured = capsys.readouterr()
    assert "vector index was not rebuilt" in captured.out
    assert "sqlite-vec is not installed" in captured.err


def test_cli_rebuild_json_carries_the_warning(tmp_path: Path, capsys, no_vectors: None) -> None:
    db_path = tmp_path / "ledger.sqlite"
    main(["--db", str(db_path), "add", SUBJECT, "--body", "x"])
    capsys.readouterr()

    main(["--db", str(db_path), "rebuild", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert result["vectors"]["available"] is False
    assert any("vector index" in warning for warning in result["warnings"])


def test_cli_vector_search_exits_non_zero_when_unavailable(tmp_path: Path, capsys, no_vectors: None) -> None:
    db_path = tmp_path / "ledger.sqlite"
    main(["--db", str(db_path), "add", SUBJECT, "--body", "x"])

    assert main(["--db", str(db_path), "vector-search", "x"]) == 1
    assert "unavailable" in capsys.readouterr().out


def test_mcp_rebuild_and_vector_search_warn_when_unavailable(tmp_path: Path, no_vectors: None) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")
    tool(server, "decision_add_record", {"subject": SUBJECT, "body": "x"})

    rebuilt = tool(server, "decision_rebuild_projection", {})
    searched = tool(server, "decision_vector_search", {"query": "x"})

    assert any("vector index" in warning for warning in rebuilt["warnings"])
    assert searched["available"] is False
    assert "unavailable" in searched["warning"]
