from __future__ import annotations

import json
from pathlib import Path

from decision_ledger.mcp_server import MCPServer


def request(server: MCPServer, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    responses = server.handle_wire_message(json.dumps(message))
    assert len(responses) == 1
    return responses[0]


def tool_call(server: MCPServer, name: str, arguments: dict, request_id: int = 10) -> dict:
    response = request(
        server,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=request_id,
    )
    assert "result" in response
    return response["result"]["structuredContent"]


def test_initialize_lists_tools_and_prompts(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    initialized = request(
        server,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )

    result = initialized["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert "Supersede" in result["instructions"] or "supersede" in result["instructions"]

    tools = request(server, "tools/list")["result"]["tools"]
    tool_names = {tool["name"] for tool in tools}
    assert "decision_add_record" in tool_names
    assert "decision_supersede_subject_before" in tool_names
    assert "decision_list_topics" in tool_names
    assert "decision_export_wiki" in tool_names
    assert all("inputSchema" in tool for tool in tools)

    prompts = request(server, "prompts/list")["result"]["prompts"]
    assert {prompt["name"] for prompt in prompts} == {
        "decision-ledger-best-practices",
        "capture-decision-context",
    }


def test_prompt_get_bakes_in_usage_guidance(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    response = request(
        server,
        "prompts/get",
        {
            "name": "capture-decision-context",
            "arguments": {"subject": "connected-ai.auth"},
        },
    )

    text = response["result"]["messages"][0]["content"]["text"]
    assert "Subject focus: connected-ai.auth" in text
    assert "Gather current context" in text
    assert "existing markdown contains durable decisions" in text
    assert "Preserve detail" in text
    assert "independent subjects, tags, evidence, statuses, or future" in text
    assert "Do not delete audit history" in text


def test_mcp_tool_calls_cover_record_flow(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    old = tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.auth.oidc",
            "kind": "thought",
            "summary": "Old thought",
            "body": "Older OIDC idea.",
            "tags": ["oidc"],
        },
    )["id"]
    new = tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.auth.oidc",
            "kind": "decision",
            "status": "accepted",
            "summary": "New decision",
            "body": "New OIDC direction.",
        },
    )["id"]

    evidence = tool_call(
        server,
        "decision_add_evidence",
        {
            "record_id": new,
            "type": "url",
            "uri": "https://example.test/evidence",
            "note": "Example evidence",
        },
    )
    assert evidence["id"].startswith("evd_")

    superseded = tool_call(
        server,
        "decision_supersede_record",
        {
            "old_record_id": old,
            "replacement_record_id": new,
            "note": "New replaces old",
        },
    )
    assert superseded["superseded"] == [old]

    gathered = tool_call(server, "decision_gather", {"subject": "connected-ai.auth", "include_obsolete": True})
    assert [row["id"] for row in gathered["current"]] == [new]
    assert [row["id"] for row in gathered["obsolete"]] == [old]
    assert gathered["evidence"][0]["uri"] == "https://example.test/evidence"


def test_batch_response_is_single_json_rpc_array(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")
    responses = server.handle_wire_message(
        json.dumps(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
    )

    assert len(responses) == 1
    assert isinstance(responses[0], list)
    assert [response["id"] for response in responses[0]] == [1, 2]


def test_mcp_export_wiki_tool(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    record_id = tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.retrieval.wiki.page-contract",
            "summary": "Page contract",
            "body": "Wiki pages should carry enough context for retrieval.",
            "visibility": "internal",
        },
    )["id"]

    result = tool_call(
        server,
        "decision_export_wiki",
        {
            "subject": "connected-ai.retrieval",
            "output_dir": str(tmp_path / "wiki"),
            "profile": "internal",
        },
    )

    assert result["records"] == 1
    assert "assets/search-index.json" in result["files"]
    assert (tmp_path / "wiki" / "records" / record_id / "index.html").exists()


def test_mcp_list_topics_tool(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.auth.oidc",
            "body": "OIDC thought.",
        },
    )
    tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.auth.mcp",
            "body": "MCP thought.",
        },
    )

    topics = tool_call(
        server,
        "decision_list_topics",
        {
            "subject": "connected-ai.auth",
            "direct_only": True,
        },
    )["result"]

    assert [topic["subject"] for topic in topics] == [
        "connected-ai.auth",
        "connected-ai.auth.mcp",
        "connected-ai.auth.oidc",
    ]
    assert topics[0]["subtree_records"] == 2
