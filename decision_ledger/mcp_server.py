from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .db import connect
from .event_store import DEFAULT_LEDGER_HOME, EventStore, EventedLedger, LedgerPaths, resolve_ledger_paths
from .guidance import CAPTURE_PROMPT, SERVER_INSTRUCTIONS, TOOL_GUIDANCE
from .model import RECORD_KINDS, VALIDATION_STATES, json_dumps


PROTOCOL_VERSION = "2025-06-18"


JsonObject = dict[str, Any]
ToolHandler = Callable[[JsonObject], Any]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision-ledger-mcp", description="Decision Ledger MCP stdio server")
    parser.add_argument("--home", help=f"Ledger home containing events/ and ledger.sqlite (default: nearest .decision-ledger or {DEFAULT_LEDGER_HOME})")
    parser.add_argument("--db", help="SQLite projection path (default: <ledger home>/ledger.sqlite)")
    args = parser.parse_args(argv)
    server = MCPServer(paths=resolve_ledger_paths(db_path=args.db, home=args.home))
    server.serve()
    return 0


class MCPServer:
    def __init__(self, db_path: Path | None = None, *, paths: LedgerPaths | None = None):
        self.paths = paths or resolve_ledger_paths(db_path=db_path)
        db_existed = self.paths.db_path.exists()
        self.conn = connect(self.paths.db_path)
        event_store = EventStore(self.paths.home)
        self.ledger = EventedLedger(self.conn, event_store, self.paths.db_path)
        if not db_existed and event_store.has_events():
            self.ledger.rebuild()
        self.tools: dict[str, tuple[JsonObject, ToolHandler]] = build_tools(self.ledger)

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            for response in self.handle_wire_message(line):
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()

    def handle_wire_message(self, line: str) -> list[JsonObject]:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return [error_response(None, -32700, f"Parse error: {exc.msg}")]

        if isinstance(message, list):
            responses = []
            for item in message:
                response = self.handle_message(item)
                if response is not None:
                    responses.append(response)
            return [responses] if responses else []

        response = self.handle_message(message)
        return [response] if response is not None else []

    def handle_message(self, message: Any) -> JsonObject | None:
        if not isinstance(message, dict):
            return error_response(None, -32600, "Invalid request")

        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return error_response(request_id, -32600, "Invalid request")

        if "id" not in message:
            self.handle_notification(method, message.get("params") or {})
            return None

        try:
            if method == "initialize":
                return result_response(request_id, self.initialize(message.get("params") or {}))
            if method == "ping":
                return result_response(request_id, {})
            if method == "tools/list":
                return result_response(request_id, {"tools": [tool for tool, _handler in self.tools.values()]})
            if method == "tools/call":
                return result_response(request_id, self.call_tool(message.get("params") or {}))
            if method == "prompts/list":
                return result_response(request_id, {"prompts": prompts_list()})
            if method == "prompts/get":
                return result_response(request_id, get_prompt(message.get("params") or {}))
            return error_response(request_id, -32601, f"Method not found: {method}")
        except ValueError as exc:
            return error_response(request_id, -32602, str(exc))
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr)
            return error_response(request_id, -32603, str(exc))

    def handle_notification(self, method: str, _params: JsonObject) -> None:
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return
        print(f"ignored notification: {method}", file=sys.stderr)

    def initialize(self, params: JsonObject) -> JsonObject:
        requested = params.get("protocolVersion")
        protocol_version = requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": "decision-ledger",
                "title": "Decision Ledger",
                "version": __version__,
            },
            "instructions": SERVER_INSTRUCTIONS,
        }

    def call_tool(self, params: JsonObject) -> JsonObject:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")

        _definition, handler = self.tools[name]
        result = handler(arguments)
        return tool_result(result)


def build_tools(ledger: EventedLedger) -> dict[str, tuple[JsonObject, ToolHandler]]:
    return {
        "decision_guidance": (
            tool_definition(
                "decision_guidance",
                "Decision Ledger Guidance",
                TOOL_GUIDANCE["decision_guidance"],
                {},
            ),
            lambda _args: {"instructions": SERVER_INSTRUCTIONS},
        ),
        "decision_rebuild_projection": (
            tool_definition(
                "decision_rebuild_projection",
                "Rebuild SQLite Projection",
                "Rebuild the generated SQLite projection from canonical namespace JSONL event files. Use after pulling event changes from git or when the projection is missing/stale.",
                {},
            ),
            lambda _args: rebuild_projection_tool(ledger),
        ),
        "decision_add_record": (
            tool_definition(
                "decision_add_record",
                "Add Decision Ledger Record",
                TOOL_GUIDANCE["decision_add_record"],
                {
                    "subject": string_schema("Dot-separated primary subject, for example connected-ai.auth.oidc.client-persistence."),
                    "body": string_schema("Free-form thought, idea, decision, assumption, question, finding, plan, or note body."),
                    "kind": enum_schema(list(RECORD_KINDS), "Record kind."),
                    "status": enum_schema(["active", "proposed", "accepted", "rejected", "superseded", "withdrawn", "resolved", "archived"], "Record status."),
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Validation state. Keep this separate from lifecycle status."),
                    "summary": string_schema("Short human-readable summary."),
                    "tags": array_schema("Loose tags for cross-cutting lookup."),
                    "related_subjects": array_schema("Secondary subject prefixes that should also retrieve this record."),
                    "created_by": string_schema("Human or agent creating the record."),
                    "visibility": enum_schema(["private", "internal", "shareable", "public"], "Export visibility."),
                },
                required=["subject", "body"],
            ),
            lambda args: {"id": ledger.add_record(
                subject=require_str(args, "subject"),
                body=require_str(args, "body"),
                kind=args.get("kind", "thought"),
                status=args.get("status", "active"),
                validation_state=args.get("validation_state", "unvalidated"),
                summary=args.get("summary"),
                tags=list_arg(args, "tags"),
                related_subjects=list_arg(args, "related_subjects"),
                created_by=args.get("created_by"),
                export_visibility=args.get("visibility", "private"),
            )},
        ),
        "decision_add_evidence": (
            tool_definition(
                "decision_add_evidence",
                "Add Decision Evidence",
                TOOL_GUIDANCE["decision_add_evidence"],
                {
                    "record_id": string_schema("Record to attach evidence to."),
                    "type": enum_schema(["file", "url", "command", "artifact", "commit", "pr", "ticket", "log", "chat", "note"], "Evidence type."),
                    "uri": string_schema("File path, URL, command text, ticket id, commit SHA, artifact path, or other locator."),
                    "line": {"type": "integer", "description": "Optional line number for file evidence."},
                    "label": string_schema("Short display label."),
                    "note": string_schema("Why this evidence matters."),
                    "observed_at": string_schema("Timestamp for point-in-time observations."),
                    "visibility": enum_schema(["private", "internal", "shareable", "public"], "Export visibility."),
                    "created_by": string_schema("Human or agent adding the evidence."),
                },
                required=["record_id", "type", "uri"],
            ),
            lambda args: {"id": ledger.add_evidence(
                record_id=require_str(args, "record_id"),
                evidence_type=require_str(args, "type"),
                uri=require_str(args, "uri"),
                line=args.get("line"),
                label=args.get("label"),
                note=args.get("note"),
                observed_at=args.get("observed_at"),
                export_visibility=args.get("visibility", "private"),
                created_by=args.get("created_by"),
            )},
        ),
        "decision_validate_record": (
            tool_definition(
                "decision_validate_record",
                "Validate Decision Record",
                TOOL_GUIDANCE["decision_validate_record"],
                {
                    "record_id": string_schema("Record id to update."),
                    "validation_state": enum_schema(list(VALIDATION_STATES), "New validation state."),
                    "note": string_schema("Why this validation state is justified."),
                    "validated_by": string_schema("Human or agent changing validation state."),
                    "validated_at": string_schema("Optional validation timestamp."),
                },
                required=["record_id", "validation_state"],
            ),
            lambda args: validate_record_tool(ledger, args),
        ),
        "decision_associate_records": (
            tool_definition(
                "decision_associate_records",
                "Associate Decision Records",
                TOOL_GUIDANCE["decision_associate_records"],
                {
                    "from_record_id": string_schema("Source record id."),
                    "to_record_id": string_schema("Target record id."),
                    "relation": enum_schema([
                        "associated_with", "supersedes", "supports", "contradicts",
                        "depends_on", "derived_from", "duplicates", "clarifies",
                        "blocks", "implements", "raises_question", "answers_question",
                    ], "Association relation."),
                    "note": string_schema("Why the records are associated."),
                    "strength": {"type": "number", "description": "Optional association strength."},
                    "source": enum_schema(["manual", "agent", "import", "inferred"], "Association source."),
                    "created_by": string_schema("Human or agent adding the association."),
                },
                required=["from_record_id", "to_record_id"],
            ),
            lambda args: {"id": ledger.associate(
                from_record_id=require_str(args, "from_record_id"),
                to_record_id=require_str(args, "to_record_id"),
                relation=args.get("relation", "associated_with"),
                note=args.get("note"),
                strength=args.get("strength"),
                source=args.get("source", "manual"),
                created_by=args.get("created_by"),
            )},
        ),
        "decision_supersede_record": (
            tool_definition(
                "decision_supersede_record",
                "Supersede One Decision Record",
                TOOL_GUIDANCE["decision_supersede_record"],
                {
                    "old_record_id": string_schema("Record that should no longer guide current reasoning."),
                    "replacement_record_id": string_schema("Newer record that replaces the old one."),
                    "note": string_schema("Why the older record is superseded."),
                    "created_by": string_schema("Human or agent performing the supersession."),
                    "valid_until": string_schema("Optional validity cutoff timestamp."),
                },
                required=["old_record_id", "replacement_record_id"],
            ),
            lambda args: supersede_record_tool(ledger, args),
        ),
        "decision_supersede_subject_before": (
            tool_definition(
                "decision_supersede_subject_before",
                "Supersede Subject Records Before Time",
                TOOL_GUIDANCE["decision_supersede_subject_before"],
                {
                    "subject": string_schema("Subject prefix to supersede under."),
                    "before": string_schema("Cutoff timestamp. Records created before this are superseded."),
                    "replacement_record_id": string_schema("Newer record that supersedes the matching records."),
                    "note": string_schema("Why the older records are superseded."),
                    "created_by": string_schema("Human or agent performing the supersession."),
                },
                required=["subject", "before", "replacement_record_id"],
            ),
            lambda args: {"superseded": ledger.supersede_subject_before(
                subject=require_str(args, "subject"),
                before=require_str(args, "before"),
                replacement_record_id=require_str(args, "replacement_record_id"),
                note=args.get("note"),
                created_by=args.get("created_by"),
            )},
        ),
        "decision_gather": (
            tool_definition(
                "decision_gather",
                "Gather Decision Context",
                TOOL_GUIDANCE["decision_gather"],
                {
                    "subject": string_schema("Subject prefix to gather."),
                    "include_obsolete": {"type": "boolean", "description": "Include superseded/rejected/withdrawn/archived records."},
                },
                required=["subject"],
            ),
            lambda args: ledger.gather(
                require_str(args, "subject"),
                include_obsolete=bool(args.get("include_obsolete", False)),
            ),
        ),
        "decision_search": (
            tool_definition(
                "decision_search",
                "Search Decision Records",
                TOOL_GUIDANCE["decision_search"],
                {
                    "query": string_schema("FTS5 full-text query."),
                    "limit": {"type": "integer", "description": "Maximum result count."},
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses."},
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
                },
                required=["query"],
            ),
            lambda args: [dict(row) for row in ledger.search(
                require_str(args, "query"),
                limit=int(args.get("limit", 20)),
                include_obsolete=bool(args.get("include_obsolete", False)),
                validation_state=args.get("validation_state"),
            )],
        ),
        "decision_show_record": (
            tool_definition(
                "decision_show_record",
                "Show Decision Record",
                TOOL_GUIDANCE["decision_show_record"],
                {"record_id": string_schema("Record id to show.")},
                required=["record_id"],
            ),
            lambda args: require_record_result(ledger, require_str(args, "record_id")),
        ),
        "decision_list_records": (
            tool_definition(
                "decision_list_records",
                "List Decision Records",
                TOOL_GUIDANCE["decision_list_records"],
                {
                    "subject": string_schema("Optional subject prefix."),
                    "status": string_schema("Optional exact status filter."),
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses."},
                    "limit": {"type": "integer", "description": "Maximum result count."},
                },
            ),
            lambda args: [dict(row) for row in ledger.list_records(
                subject=args.get("subject"),
                status=args.get("status"),
                validation_state=args.get("validation_state"),
                include_obsolete=bool(args.get("include_obsolete", False)),
                limit=int(args.get("limit", 50)),
            )],
        ),
        "decision_list_topics": (
            tool_definition(
                "decision_list_topics",
                "List Decision Topics",
                TOOL_GUIDANCE["decision_list_topics"],
                {
                    "subject": string_schema("Optional subject prefix to list under."),
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete records in topic counts."},
                    "direct_only": {"type": "boolean", "description": "Only return the prefix and its direct child topics."},
                },
            ),
            lambda args: ledger.list_topics(
                subject=args.get("subject"),
                include_obsolete=bool(args.get("include_obsolete", False)),
                direct_only=bool(args.get("direct_only", False)),
            ),
        ),
    }


def tool_definition(
    name: str,
    title: str,
    description: str,
    properties: JsonObject,
    *,
    required: list[str] | None = None,
) -> JsonObject:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def string_schema(description: str) -> JsonObject:
    return {"type": "string", "description": description}


def enum_schema(values: list[str], description: str) -> JsonObject:
    return {"type": "string", "enum": values, "description": description}


def array_schema(description: str) -> JsonObject:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def tool_result(value: Any) -> JsonObject:
    text = json_dumps(value)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value if isinstance(value, dict) else {"result": value},
        "isError": False,
    }


def rebuild_projection_tool(ledger: EventedLedger) -> JsonObject:
    ledger.rebuild()
    return {"rebuilt": True, "db_path": str(ledger.db_path), "events_dir": str(ledger.event_store.events_dir)}


def supersede_record_tool(ledger: EventedLedger, args: JsonObject) -> JsonObject:
    old_record_id = require_str(args, "old_record_id")
    ledger.supersede_record(
        old_record_id=old_record_id,
        replacement_record_id=require_str(args, "replacement_record_id"),
        note=args.get("note"),
        created_by=args.get("created_by"),
        valid_until=args.get("valid_until"),
    )
    return {"superseded": [old_record_id]}


def validate_record_tool(ledger: EventedLedger, args: JsonObject) -> JsonObject:
    record_id = require_str(args, "record_id")
    validation_state = require_str(args, "validation_state")
    ledger.validate_record(
        record_id=record_id,
        validation_state=validation_state,
        note=args.get("note"),
        validated_by=args.get("validated_by"),
        validated_at=args.get("validated_at"),
    )
    return {"record_id": record_id, "validation_state": validation_state}


def require_record_result(ledger: EventedLedger, record_id: str) -> JsonObject:
    record = ledger.get_record(record_id)
    if record is None:
        raise ValueError(f"record not found: {record_id}")
    return record


def require_str(args: JsonObject, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def list_arg(args: JsonObject, name: str) -> list[str]:
    value = args.get(name) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return value


def prompts_list() -> list[JsonObject]:
    return [
        {
            "name": "decision-ledger-best-practices",
            "title": "Decision Ledger Best Practices",
            "description": "Skill-like instructions for using the Decision Ledger MCP surface.",
            "arguments": [],
        },
        {
            "name": "capture-decision-context",
            "title": "Capture Decision Context",
            "description": "Prompt for deciding when and how to gather, add, evidence, associate, or supersede records.",
            "arguments": [
                {
                    "name": "subject",
                    "description": "Optional subject prefix to focus the instructions.",
                    "required": False,
                }
            ],
        },
    ]


def get_prompt(params: JsonObject) -> JsonObject:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "decision-ledger-best-practices":
        text = SERVER_INSTRUCTIONS
        description = "Decision Ledger best practices"
    elif name == "capture-decision-context":
        subject = arguments.get("subject")
        prefix = f"Subject focus: {subject}\n\n" if subject else ""
        text = prefix + CAPTURE_PROMPT
        description = "Capture Decision Ledger context"
    else:
        raise ValueError(f"Unknown prompt: {name}")
    return {
        "description": description,
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            }
        ],
    }


def result_response(request_id: Any, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


if __name__ == "__main__":
    raise SystemExit(main())
