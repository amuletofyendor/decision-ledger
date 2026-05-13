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
    assert "decision_rebuild_projection" in tool_names
    assert "decision_add_record" in tool_names
    assert "decision_validate_record" in tool_names
    assert "decision_add_html_artifact" in tool_names
    assert "decision_add_image_artifact" in tool_names
    assert "decision_list_artifacts" in tool_names
    assert "decision_view_subject" in tool_names
    assert "decision_query_records" in tool_names
    assert "decision_create_view" in tool_names
    assert "decision_save_view" in tool_names
    assert "decision_list_views" in tool_names
    assert "decision_vector_search" in tool_names
    assert "decision_supersede_subject_before" in tool_names
    assert "decision_list_topics" in tool_names
    assert "decision_export_wiki" not in tool_names
    assert all("inputSchema" in tool for tool in tools)
    add_record_tool = next(tool for tool in tools if tool["name"] == "decision_add_record")
    assert "idea" in add_record_tool["inputSchema"]["properties"]["kind"]["enum"]
    assert "snag" in add_record_tool["inputSchema"]["properties"]["kind"]["enum"]

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
    assert "seems small" in text
    assert "context compaction" in text
    assert "decision-wiki-server" in text
    assert "localhost port" in text
    assert "decision_add_html_artifact" in text
    assert "decision_save_view" in text
    assert "validation_state" in text
    assert "add it as an idea" in text
    assert "Do not delete audit history" in text


def test_mcp_tool_calls_cover_record_flow(tmp_path: Path) -> None:
    server = MCPServer(tmp_path / "ledger.sqlite")

    old = tool_call(
        server,
        "decision_add_record",
        {
            "subject": "connected-ai.auth.oidc",
            "kind": "idea",
            "summary": "Old idea",
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
            "validation_state": "partially_validated",
            "summary": "New decision",
            "body": "New OIDC direction.",
        },
    )["id"]

    validated = tool_call(
        server,
        "decision_validate_record",
        {
            "record_id": new,
            "validation_state": "validated",
            "note": "Checked in MCP test",
            "validated_by": "pytest",
        },
    )
    assert validated["validation_state"] == "validated"

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

    artifact = tool_call(
        server,
        "decision_add_html_artifact",
        {
            "subject": "connected-ai.auth.oidc",
            "record_id": new,
            "html": "<!doctype html><script>window.ok=true</script><p>OIDC artifact</p>",
            "label": "OIDC artifact",
            "visibility": "internal",
        },
    )
    assert artifact["id"].startswith("art_")
    assert artifact["record_id"] == new
    listed_artifacts = tool_call(server, "decision_list_artifacts", {"subject": "connected-ai.auth"})
    assert [item["id"] for item in listed_artifacts["result"]] == [artifact["id"]]
    subject_view = tool_call(server, "decision_view_subject", {"subject": "connected-ai.auth"})
    assert artifact["id"] in {entry.get("artifact_id") for entry in subject_view["entries"]}

    queried = tool_call(
        server,
        "decision_query_records",
        {"subject": "connected-ai.auth", "kind": "decision", "include_body": True},
    )
    assert [row["id"] for row in queried["result"]] == [new]
    assert queried["result"][0]["body"] == "New OIDC direction."

    created_view = tool_call(
        server,
        "decision_create_view",
        {
            "subject": "connected-ai.auth",
            "kind": "decision",
            "title": "Auth Decisions View",
        },
    )
    assert created_view["stored"] is False
    assert created_view["record_count"] == 1
    assert "Auth Decisions View" in created_view["html"]
    assert "decision_save_view" in created_view["persistence"]

    saved_view = tool_call(
        server,
        "decision_save_view",
        {
            "subject": "decision-ledger.test-views",
            "query_subject": "connected-ai.auth",
            "kind": "decision",
            "title": "Saved Auth Decisions",
            "visibility": "internal",
        },
    )
    assert saved_view["id"].startswith("view_")
    assert saved_view["url"] == f"/saved-views/{saved_view['id']}.html"
    listed_views = tool_call(server, "decision_list_views", {"subject": "decision-ledger"})
    assert [item["id"] for item in listed_views["result"]] == [saved_view["id"]]

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

    searched = tool_call(server, "decision_search", {"query": "OIDC", "limit": 5})
    assert searched["query"] == "OIDC"
    assert searched["lexical"]["available"] is True
    assert searched["combined"][0]["id"] == new
    assert "lexical" in searched["combined"][0]["sources"]

    event_file = tmp_path / "events" / "connected-ai" / "auth" / "oidc.jsonl"
    assert event_file.exists()
    assert '"event_type":"superseded"' in event_file.read_text(encoding="utf-8")
    saved_view_event_file = tmp_path / "events" / "decision-ledger" / "test-views.jsonl"
    assert '"event_type":"view_saved"' in saved_view_event_file.read_text(encoding="utf-8")

    (tmp_path / "ledger.sqlite").unlink()
    rebuild = tool_call(server, "decision_rebuild_projection", {})
    assert rebuild["rebuilt"] is True

    rebuilt = tool_call(server, "decision_show_record", {"record_id": new})
    assert rebuilt["summary"] == "New decision"
    assert rebuilt["validation_state"] == "validated"


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
