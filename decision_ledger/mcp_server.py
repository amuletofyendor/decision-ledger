from __future__ import annotations

import argparse
import base64
import json
import sys
import traceback
from html import escape
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .db import connect
from .event_store import DEFAULT_LEDGER_HOME, EventStore, EventedLedger, LedgerPaths, resolve_ledger_paths
from .guidance import CAPTURE_PROMPT, SERVER_INSTRUCTIONS, TOOL_GUIDANCE
from .model import RECORD_KINDS, RECORD_STATUSES, VALIDATION_STATES, json_dumps


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
                {
                    "skip_vectors": {"type": "boolean", "description": "Skip rebuilding the generated vector projection."},
                },
            ),
            lambda args: rebuild_projection_tool(ledger, skip_vectors=bool(args.get("skip_vectors", False))),
        ),
        "decision_add_record": (
            tool_definition(
                "decision_add_record",
                "Add Decision Ledger Record",
                TOOL_GUIDANCE["decision_add_record"],
                {
                    "subject": string_schema("Dot-separated primary subject, for example product.auth.oauth.client-persistence."),
                    "body": string_schema("Free-form thought, idea, snag, decision, assumption, question, finding, plan, or note body."),
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
        "decision_add_html_artifact": (
            tool_definition(
                "decision_add_html_artifact",
                "Add HTML Artifact",
                TOOL_GUIDANCE["decision_add_html_artifact"],
                {
                    "subject": string_schema("Dot-separated subject for the artifact record."),
                    "html": string_schema("Complete HTML content to store. Inline CSS and inline JavaScript are allowed in this trusted local/team ledger."),
                    "record_id": string_schema("Optional existing record to attach the artifact to. If omitted, a note record is created."),
                    "label": string_schema("Short artifact label."),
                    "summary": string_schema("Summary for the created record and artifact listing."),
                    "body": string_schema("Optional body for the created record."),
                    "tags": array_schema("Loose tags for the created artifact record."),
                    "related_subjects": array_schema("Secondary subject prefixes for the created artifact record."),
                    "created_by": string_schema("Human or agent creating the artifact."),
                    "visibility": enum_schema(["private", "internal", "shareable", "public"], "Export visibility."),
                },
                required=["subject", "html"],
            ),
            lambda args: ledger.add_artifact(
                subject=require_str(args, "subject"),
                artifact_type="html",
                content=require_str(args, "html").encode("utf-8"),
                extension=".html",
                content_type="text/html; charset=utf-8",
                label=args.get("label"),
                summary=args.get("summary"),
                body=args.get("body"),
                record_id=args.get("record_id"),
                tags=list_arg(args, "tags"),
                related_subjects=list_arg(args, "related_subjects"),
                created_by=args.get("created_by"),
                export_visibility=args.get("visibility", "private"),
            ),
        ),
        "decision_add_image_artifact": (
            tool_definition(
                "decision_add_image_artifact",
                "Add Image Artifact",
                TOOL_GUIDANCE["decision_add_image_artifact"],
                {
                    "subject": string_schema("Dot-separated subject for the artifact record."),
                    "source_path": string_schema("Local image path to copy into the ledger."),
                    "base64_content": string_schema("Base64-encoded image bytes. Use this when source_path is not available."),
                    "extension": string_schema("File extension for base64_content, for example .png."),
                    "content_type": string_schema("Optional image content type override, for example image/png."),
                    "record_id": string_schema("Optional existing record to attach the artifact to. If omitted, a note record is created."),
                    "label": string_schema("Short artifact label."),
                    "summary": string_schema("Summary for the created record and artifact listing."),
                    "body": string_schema("Optional body for the created record."),
                    "tags": array_schema("Loose tags for the created artifact record."),
                    "related_subjects": array_schema("Secondary subject prefixes for the created artifact record."),
                    "created_by": string_schema("Human or agent creating the artifact."),
                    "visibility": enum_schema(["private", "internal", "shareable", "public"], "Export visibility."),
                },
                required=["subject"],
            ),
            lambda args: add_image_artifact_tool(ledger, args),
        ),
        "decision_list_artifacts": (
            tool_definition(
                "decision_list_artifacts",
                "List Artifacts",
                TOOL_GUIDANCE["decision_list_artifacts"],
                {
                    "subject": string_schema("Optional subject prefix."),
                    "type": enum_schema(["html", "image"], "Optional artifact type."),
                    "include_obsolete": {"type": "boolean", "description": "Include artifacts attached to obsolete records."},
                    "limit": {"type": "integer", "description": "Maximum result count."},
                },
            ),
            lambda args: [dict(row) for row in ledger.list_artifacts(
                subject=args.get("subject"),
                artifact_type=args.get("type"),
                include_obsolete=bool(args.get("include_obsolete", False)),
                limit=int(args.get("limit", 100)),
            )],
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
        "decision_view_subject": (
            tool_definition(
                "decision_view_subject",
                "Build Subject View",
                TOOL_GUIDANCE["decision_view_subject"],
                {
                    "subject": string_schema("Subject prefix to view."),
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete records."},
                    "limit": {"type": "integer", "description": "Maximum record count to source before adding artifacts."},
                },
                required=["subject"],
            ),
            lambda args: ledger.subject_view(
                require_str(args, "subject"),
                include_obsolete=bool(args.get("include_obsolete", False)),
                limit=int(args.get("limit", 200)),
            ),
        ),
        "decision_query_records": (
            tool_definition(
                "decision_query_records",
                "Query Decision Records",
                TOOL_GUIDANCE["decision_query_records"],
                query_record_schema(),
            ),
            lambda args: ledger.query_records(**query_record_args(args)),
        ),
        "decision_create_view": (
            tool_definition(
                "decision_create_view",
                "Create Filtered View",
                TOOL_GUIDANCE["decision_create_view"],
                {
                    **query_record_schema(),
                    "subject": string_schema("Subject prefix filter for the view."),
                    "title": string_schema("View title."),
                    "render_html": {"type": "boolean", "description": "Include transient rendered HTML in the response. This does not store an artifact."},
                },
                required=["subject"],
            ),
            lambda args: create_view_tool(ledger, args),
        ),
        "decision_save_view": (
            tool_definition(
                "decision_save_view",
                "Save View Definition",
                TOOL_GUIDANCE["decision_save_view"],
                {
                    **query_record_schema(),
                    "subject": string_schema("Subject under which this saved view belongs. Defaults the query subject unless query_subject is supplied."),
                    "query_subject": string_schema("Optional subject prefix filter for the records included by the saved view."),
                    "title": string_schema("Saved view title."),
                    "created_by": string_schema("Human or agent saving the view definition."),
                    "visibility": enum_schema(["private", "internal", "shareable", "public"], "Export visibility."),
                },
                required=["subject", "title"],
            ),
            lambda args: save_view_tool(ledger, args),
        ),
        "decision_list_views": (
            tool_definition(
                "decision_list_views",
                "List Saved Views",
                TOOL_GUIDANCE["decision_list_views"],
                {
                    "subject": string_schema("Optional saved-view subject prefix."),
                    "limit": {"type": "integer", "description": "Maximum result count."},
                },
            ),
            lambda args: ledger.list_saved_views(
                subject=args.get("subject"),
                limit=int(args.get("limit", 100)),
            ),
        ),
        "decision_search": (
            tool_definition(
                "decision_search",
                "Search Decision Records",
                TOOL_GUIDANCE["decision_search"],
                {
                    "query": string_schema("Search query. Results combine lexical and vector retrieval when vector search is available."),
                    "limit": {"type": "integer", "description": "Maximum result count."},
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses."},
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
                },
                required=["query"],
            ),
            lambda args: ledger.hybrid_search(
                require_str(args, "query"),
                limit=int(args.get("limit", 20)),
                include_obsolete=bool(args.get("include_obsolete", False)),
                validation_state=args.get("validation_state"),
            ),
        ),
        "decision_vector_search": (
            tool_definition(
                "decision_vector_search",
                "Vector Search Decision Records",
                "Semantic vector search over record subject, metadata, tags, related subjects, summary, and body. Uses the generated SQLite vector projection and local Ollama embeddings when available.",
                {
                    "query": string_schema("Natural-language semantic search query."),
                    "limit": {"type": "integer", "description": "Maximum result count."},
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses."},
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
                },
                required=["query"],
            ),
            lambda args: ledger.vector_search(
                require_str(args, "query"),
                limit=int(args.get("limit", 20)),
                include_obsolete=bool(args.get("include_obsolete", False)),
                validation_state=args.get("validation_state"),
            ),
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
                    "kind": enum_schema(list(RECORD_KINDS), "Optional record kind filter."),
                    "status": enum_schema(list(RECORD_STATUSES), "Optional exact status filter."),
                    "exclude_status": array_schema("Statuses to exclude."),
                    "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
                    "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses."},
                    "limit": {"type": "integer", "description": "Maximum result count."},
                },
            ),
            lambda args: [dict(row) for row in ledger.list_records(
                subject=args.get("subject"),
                kind=args.get("kind"),
                status=args.get("status"),
                exclude_status=list_arg(args, "exclude_status"),
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


def enum_array_schema(values: list[str], description: str) -> JsonObject:
    return {"type": "array", "items": {"type": "string", "enum": values}, "description": description}


def query_record_schema() -> JsonObject:
    return {
        "subject": string_schema("Optional subject prefix filter."),
        "kind": enum_schema(list(RECORD_KINDS), "Optional record kind filter."),
        "status": enum_array_schema(list(RECORD_STATUSES), "Optional statuses to include."),
        "exclude_status": enum_array_schema(list(RECORD_STATUSES), "Statuses to exclude."),
        "validation_state": enum_schema(list(VALIDATION_STATES), "Optional validation-state filter."),
        "tags": array_schema("Tags that must all be present."),
        "created_from": string_schema("Optional lower created_at bound."),
        "created_to": string_schema("Optional upper created_at bound."),
        "include_obsolete": {"type": "boolean", "description": "Include obsolete statuses unless status is explicitly supplied."},
        "include_body": {"type": "boolean", "description": "Include record body text."},
        "include_evidence": {"type": "boolean", "description": "Include evidence arrays."},
        "include_artifacts": {"type": "boolean", "description": "Include artifact arrays."},
        "limit": {"type": "integer", "description": "Maximum result count."},
        "sort": enum_schema(["created_desc", "created_asc", "subject"], "Sort order."),
    }


def query_record_args(args: JsonObject) -> JsonObject:
    return {
        "subject": args.get("query_subject") or args.get("subject"),
        "kind": args.get("kind"),
        "status": list_arg(args, "status"),
        "exclude_status": list_arg(args, "exclude_status"),
        "validation_state": args.get("validation_state"),
        "tags": list_arg(args, "tags"),
        "created_from": args.get("created_from"),
        "created_to": args.get("created_to"),
        "include_obsolete": bool(args.get("include_obsolete", False)),
        "include_body": bool(args.get("include_body", False)),
        "include_evidence": bool(args.get("include_evidence", False)),
        "include_artifacts": bool(args.get("include_artifacts", False)),
        "limit": int(args.get("limit", 50)),
        "sort": args.get("sort", "created_desc"),
    }


def tool_result(value: Any) -> JsonObject:
    text = json_dumps(value)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value if isinstance(value, dict) else {"result": value},
        "isError": False,
    }


def rebuild_projection_tool(ledger: EventedLedger, *, skip_vectors: bool = False) -> JsonObject:
    ledger.rebuild()
    vector_result = None if skip_vectors else ledger.rebuild_vectors()
    return {
        "rebuilt": True,
        "db_path": str(ledger.db_path),
        "events_dir": str(ledger.event_store.events_dir),
        "vectors": vector_result,
    }


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


def add_image_artifact_tool(ledger: EventedLedger, args: JsonObject) -> JsonObject:
    source_path = args.get("source_path")
    base64_content = args.get("base64_content")
    if source_path:
        path = Path(str(source_path)).expanduser()
        content = path.read_bytes()
        extension = path.suffix
        source_uri = str(path)
        label = args.get("label") or path.name
    elif base64_content:
        content = base64.b64decode(str(base64_content))
        extension = args.get("extension") or ".img"
        source_uri = None
        label = args.get("label")
    else:
        raise ValueError("source_path or base64_content is required")
    return ledger.add_artifact(
        subject=require_str(args, "subject"),
        artifact_type="image",
        content=content,
        extension=str(extension),
        content_type=args.get("content_type"),
        label=label,
        summary=args.get("summary"),
        body=args.get("body"),
        source_uri=source_uri,
        record_id=args.get("record_id"),
        tags=list_arg(args, "tags"),
        related_subjects=list_arg(args, "related_subjects"),
        created_by=args.get("created_by"),
        export_visibility=args.get("visibility", "private"),
    )


def create_view_tool(ledger: EventedLedger, args: JsonObject) -> JsonObject:
    subject = require_str(args, "subject")
    query_args = query_record_args(args)
    query_args["subject"] = subject
    if "include_body" not in args:
        query_args["include_body"] = True
    if "include_evidence" not in args:
        query_args["include_evidence"] = True
    if "include_artifacts" not in args:
        query_args["include_artifacts"] = True
    records = ledger.query_records(**query_args)
    title = args.get("title") or f"Decision Ledger View: {query_args.get('subject') or 'all subjects'}"
    result: JsonObject = {
        "title": title,
        "query": query_args,
        "records": records,
        "record_count": len(records),
        "stored": False,
        "persistence": "Call decision_save_view to persist a reusable view definition. HTML artifacts are ancillary data, not saved views.",
    }
    if bool(args.get("render_html", True)):
        result["html"] = render_query_view_html(title, records, query_args)
    return result


def save_view_tool(ledger: EventedLedger, args: JsonObject) -> JsonObject:
    subject = require_str(args, "subject")
    query_args = query_record_args(args)
    if not args.get("query_subject"):
        query_args["subject"] = subject
    if "include_body" not in args:
        query_args["include_body"] = True
    if "include_evidence" not in args:
        query_args["include_evidence"] = True
    if "include_artifacts" not in args:
        query_args["include_artifacts"] = True
    return ledger.save_view(
        subject=subject,
        title=require_str(args, "title"),
        query=query_args,
        created_by=args.get("created_by"),
        export_visibility=args.get("visibility", "private"),
    )


def render_query_view_html(title: str, records: list[dict[str, Any]], query_args: JsonObject) -> str:
    body = [
        "<!doctype html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{html_escape(title)}</title>",
        "<style>",
        "body{font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#202124;background:#f7f7f4}",
        "main{max-width:1120px;margin:0 auto}.record{background:#fff;border:1px solid #d8d7d0;border-radius:6px;padding:14px;margin:12px 0}",
        ".meta{color:#5f6368}.badge{display:inline-block;border:1px solid #d8d7d0;border-radius:999px;padding:1px 8px;font-size:12px;margin-right:4px;color:#5f6368}",
        "pre{white-space:pre-wrap;background:#efeee8;border:1px solid #d8d7d0;border-radius:6px;padding:12px;overflow:auto}",
        "a{color:#226f68;text-decoration:none}a:hover{text-decoration:underline}",
        "</style>",
        "</head>",
        "<body><main>",
        f"<h1>{html_escape(title)}</h1>",
        f"<p class=\"meta\">Records: {len(records)}</p>",
        f"<pre>{html_escape(json.dumps(query_args, indent=2, sort_keys=True))}</pre>",
    ]
    for record in records:
        body.extend(
            [
                "<section class=\"record\">",
                f"<div><span class=\"badge\">{html_escape(record['kind'])}</span><span class=\"badge\">{html_escape(record['status'])}</span><span class=\"badge\">{html_escape(record['validation_state'])}</span></div>",
                f"<h2>{html_escape(record.get('summary') or record['id'])}</h2>",
                f"<p class=\"meta\">{html_escape(record['subject'])} · {html_escape(record['created_at'])} · {html_escape(record['id'])}</p>",
            ]
        )
        if record.get("body"):
            body.append(f"<pre>{html_escape(record['body'])}</pre>")
        if record.get("evidence"):
            body.append("<h3>Evidence</h3><ul>")
            for item in record["evidence"]:
                body.append(f"<li>{html_escape(item['type'])}: {html_escape(item['uri'])}</li>")
            body.append("</ul>")
        if record.get("artifacts"):
            body.append("<h3>Artifacts</h3><ul>")
            for item in record["artifacts"]:
                body.append(f"<li>{html_escape(item['type'])}: {html_escape(item.get('label') or item['id'])}</li>")
            body.append("</ul>")
        body.append("</section>")
    body.extend(["</main></body>", "</html>", ""])
    return "\n".join(body)


def html_escape(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


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
