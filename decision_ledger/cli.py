from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .db import connect
from .event_store import DEFAULT_LEDGER_HOME, EventStore, EventedLedger, LedgerPaths, resolve_ledger_paths
from .model import ARTIFACT_TYPES, ASSOCIATION_RELATIONS, RECORD_KINDS, RECORD_STATUSES, VALIDATION_STATES, WIP_AREAS, WIP_STATUSES, json_dumps


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_ledger_paths(db_path=args.db, home=args.home)
    try:
        db_existed = paths.db_path.exists()
        conn = connect(paths.db_path)
        event_store = EventStore(paths.home)
        ledger = EventedLedger(conn, event_store, paths.db_path)
        if not db_existed and event_store.has_events():
            ledger.rebuild()
        return args.func(args, ledger, paths)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decisions", description="Decision Ledger CLI")
    parser.add_argument("--home", help=f"Ledger home containing events/ and ledger.sqlite (default: nearest .decision-ledger or {DEFAULT_LEDGER_HOME})")
    parser.add_argument("--db", help="SQLite projection path (default: <ledger home>/ledger.sqlite)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create or migrate the event store and SQLite projection")
    init.set_defaults(func=cmd_init)

    rebuild = subparsers.add_parser("rebuild", help="Rebuild the SQLite projection from canonical namespace event files")
    rebuild.add_argument("--skip-vectors", action="store_true", help="Skip rebuilding the generated vector projection")
    rebuild.add_argument("--json", action="store_true")
    rebuild.set_defaults(func=cmd_rebuild)

    add = subparsers.add_parser("add", help="Add a record")
    add.add_argument("subject")
    add.add_argument("--kind", default="thought", choices=RECORD_KINDS)
    add.add_argument("--status", default="active", choices=["active", "proposed", "accepted", "rejected", "superseded", "withdrawn", "resolved", "archived"])
    add.add_argument("--validation-state", default="unvalidated", choices=VALIDATION_STATES)
    add.add_argument("--summary")
    add.add_argument("--body")
    add.add_argument("--body-file")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--related-subject", action="append", default=[])
    add.add_argument("--created-by")
    add.add_argument("--visibility", default="private", choices=["private", "internal", "shareable", "public"])
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=cmd_add)

    list_cmd = subparsers.add_parser("list", help="List records")
    list_cmd.add_argument("subject", nargs="?")
    list_cmd.add_argument("--kind", choices=RECORD_KINDS)
    list_cmd.add_argument("--status", choices=RECORD_STATUSES)
    list_cmd.add_argument("--exclude-status", action="append", default=[], choices=RECORD_STATUSES)
    list_cmd.add_argument("--validation-state", choices=VALIDATION_STATES)
    list_cmd.add_argument("--all", action="store_true", help="Include obsolete records")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    wip = subparsers.add_parser("wip", help="Manage temporal personal and work TODOs")
    wip_sub = wip.add_subparsers(dest="wip_command", required=True)
    wip_add = wip_sub.add_parser("add", help="Quick-capture a WIP item")
    wip_add.add_argument("title")
    wip_add.add_argument("--area", required=True, choices=WIP_AREAS)
    wip_add.add_argument("--subject", help="Ledger subject; defaults to wip.<area>")
    wip_add.add_argument("--body")
    wip_add.add_argument("--body-file")
    wip_add.add_argument("--expected-by", help="ISO 8601 date or timezone-aware datetime")
    wip_add.add_argument("--created-by")
    wip_add.add_argument("--visibility", default="private", choices=["private", "internal", "shareable", "public"])
    wip_add.add_argument("--json", action="store_true")
    wip_add.set_defaults(func=cmd_wip_add)

    wip_list = wip_sub.add_parser("list", help="List WIP items; open items are shown by default")
    wip_list.add_argument("--area", choices=WIP_AREAS)
    wip_list.add_argument("--subject")
    wip_list.add_argument("--status", choices=WIP_STATUSES)
    wip_list.add_argument("--all", action="store_true", help="Include closed items")
    wip_list.add_argument("--expected-from")
    wip_list.add_argument("--expected-to")
    wip_list.add_argument("--unscheduled", action="store_true")
    wip_list.add_argument("--limit", type=int, default=200)
    wip_list.add_argument("--json", action="store_true")
    wip_list.set_defaults(func=cmd_wip_list)

    wip_show = wip_sub.add_parser("show", help="Show a WIP item with record links and audit events")
    wip_show.add_argument("work_item_id")
    wip_show.add_argument("--json", action="store_true")
    wip_show.set_defaults(func=cmd_wip_show)

    wip_update = wip_sub.add_parser("update", help="Update or schedule a WIP item")
    wip_update.add_argument("work_item_id")
    wip_update.add_argument("--area", choices=WIP_AREAS)
    wip_update.add_argument("--subject")
    wip_update.add_argument("--title")
    wip_update.add_argument("--body")
    wip_update.add_argument("--body-file")
    wip_update.add_argument("--status", choices=WIP_STATUSES)
    wip_update.add_argument("--expected-by")
    wip_update.add_argument("--clear-expected-by", action="store_true")
    wip_update.add_argument("--visibility", choices=["private", "internal", "shareable", "public"])
    wip_update.add_argument("--note")
    wip_update.add_argument("--updated-by")
    wip_update.add_argument("--json", action="store_true")
    wip_update.set_defaults(func=cmd_wip_update)

    for command, status in (("close", "closed"), ("reopen", "open")):
        state_parser = wip_sub.add_parser(command, help=f"Mark a WIP item {status}")
        state_parser.add_argument("work_item_id")
        state_parser.add_argument("--note")
        state_parser.add_argument("--updated-by")
        state_parser.add_argument("--json", action="store_true")
        state_parser.set_defaults(func=cmd_wip_set_status, target_status=status)

    wip_link = wip_sub.add_parser("link", help="Link a WIP item to a durable ledger record")
    wip_link.add_argument("work_item_id")
    wip_link.add_argument("record_id")
    wip_link.add_argument("--relation", default="associated_with", choices=[item for item in ASSOCIATION_RELATIONS if item != "supersedes"])
    wip_link.add_argument("--note")
    wip_link.add_argument("--created-by")
    wip_link.add_argument("--json", action="store_true")
    wip_link.set_defaults(func=cmd_wip_link)

    topics = subparsers.add_parser("topics", help="List subject topics in the decision tree")
    topics.add_argument("subject", nargs="?", help="Optional subject prefix to list under")
    topics.add_argument("--all", action="store_true", help="Include obsolete records in topic counts")
    topics.add_argument("--direct", action="store_true", help="Only show the prefix and its direct child topics")
    topics.add_argument("--json", action="store_true")
    topics.set_defaults(func=cmd_topics)

    show = subparsers.add_parser("show", help="Show a record")
    show.add_argument("record_id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    search = subparsers.add_parser("search", help="Full-text search records")
    search.add_argument("query")
    search.add_argument("--all", action="store_true", help="Include obsolete records")
    search.add_argument("--validation-state", choices=VALIDATION_STATES)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    vector_search = subparsers.add_parser("vector-search", help="Vector search records with local embeddings")
    vector_search.add_argument("query")
    vector_search.add_argument("--all", action="store_true", help="Include obsolete records")
    vector_search.add_argument("--validation-state", choices=VALIDATION_STATES)
    vector_search.add_argument("--limit", type=int, default=20)
    vector_search.add_argument("--json", action="store_true")
    vector_search.set_defaults(func=cmd_vector_search)

    gather = subparsers.add_parser("gather", help="Gather context for a subject prefix")
    gather.add_argument("subject")
    gather.add_argument("--all", action="store_true", help="Include obsolete records")
    gather.add_argument("--json", action="store_true")
    gather.set_defaults(func=cmd_gather)

    view = subparsers.add_parser("view", help="Build a dated subject view containing records and artifacts")
    view.add_argument("subject")
    view.add_argument("--all", action="store_true", help="Include obsolete records")
    view.add_argument("--limit", type=int, default=200)
    view.add_argument("--json", action="store_true")
    view.set_defaults(func=cmd_view)

    coverage = subparsers.add_parser("coverage", help="Report simple coverage and consistency gaps")
    coverage.add_argument("subject", nargs="?")
    coverage.add_argument("--all", action="store_true", help="Include obsolete records")
    coverage.add_argument("--limit", type=int, default=100)
    coverage.add_argument("--json", action="store_true")
    coverage.set_defaults(func=cmd_coverage)

    evidence = subparsers.add_parser("evidence", help="Manage evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_sub.add_parser("add", help="Attach evidence to a record")
    evidence_add.add_argument("record_id")
    evidence_add.add_argument("--type", required=True, choices=["file", "url", "command", "artifact", "commit", "pr", "ticket", "log", "chat", "note"])
    evidence_add.add_argument("--uri", required=True)
    evidence_add.add_argument("--line", type=int)
    evidence_add.add_argument("--label")
    evidence_add.add_argument("--note")
    evidence_add.add_argument("--observed-at")
    evidence_add.add_argument("--visibility", default="private", choices=["private", "internal", "shareable", "public"])
    evidence_add.add_argument("--created-by")
    evidence_add.add_argument("--json", action="store_true")
    evidence_add.set_defaults(func=cmd_evidence_add)

    artifact = subparsers.add_parser("artifact", help="Manage stored HTML and image artifacts")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_html = artifact_sub.add_parser("add-html", help="Store a self-contained HTML artifact")
    add_artifact_arguments(artifact_html)
    artifact_html.set_defaults(func=cmd_artifact_add_html)
    artifact_image = artifact_sub.add_parser("add-image", help="Store an image artifact")
    add_artifact_arguments(artifact_image)
    artifact_image.add_argument("--content-type", help="Override detected image content type")
    artifact_image.set_defaults(func=cmd_artifact_add_image)
    artifact_text = artifact_sub.add_parser("add-text", help="Store a focused text/code-like artifact")
    artifact_text.add_argument("subject")
    artifact_text.add_argument("--type", required=True, choices=[item for item in ARTIFACT_TYPES if item not in {"html", "image"}])
    artifact_text.add_argument("--file", help="Artifact file to copy into the ledger")
    artifact_text.add_argument("--content", help="Inline artifact content")
    artifact_text.add_argument("--extension", help="Storage extension, for example .md or .py")
    artifact_text.add_argument("--content-type", help="Override detected content type")
    artifact_text.add_argument("--record-id", help="Attach artifact to an existing record instead of creating a new record")
    artifact_text.add_argument("--label")
    artifact_text.add_argument("--summary")
    artifact_text.add_argument("--body")
    artifact_text.add_argument("--source-uri")
    artifact_text.add_argument("--tag", action="append", default=[])
    artifact_text.add_argument("--related-subject", action="append", default=[])
    artifact_text.add_argument("--created-by")
    artifact_text.add_argument("--visibility", default="private", choices=["private", "internal", "shareable", "public"])
    artifact_text.add_argument("--json", action="store_true")
    artifact_text.set_defaults(func=cmd_artifact_add_text)
    artifact_associate = artifact_sub.add_parser("associate", help="Associate a record with an artifact")
    artifact_associate.add_argument("record_id")
    artifact_associate.add_argument("artifact_id")
    artifact_associate.add_argument("--relation", default="associated_with", choices=[item for item in ASSOCIATION_RELATIONS if item != "supersedes"])
    artifact_associate.add_argument("--note")
    artifact_associate.add_argument("--strength", type=float)
    artifact_associate.add_argument("--source", default="manual", choices=["manual", "agent", "import", "inferred"])
    artifact_associate.add_argument("--created-by")
    artifact_associate.add_argument("--json", action="store_true")
    artifact_associate.set_defaults(func=cmd_artifact_associate)
    artifact_list = artifact_sub.add_parser("list", help="List stored artifacts")
    artifact_list.add_argument("subject", nargs="?")
    artifact_list.add_argument("--type", choices=ARTIFACT_TYPES)
    artifact_list.add_argument("--all", action="store_true", help="Include artifacts attached to obsolete records")
    artifact_list.add_argument("--limit", type=int, default=100)
    artifact_list.add_argument("--json", action="store_true")
    artifact_list.set_defaults(func=cmd_artifact_list)

    validate = subparsers.add_parser("validate", help="Change a record validation state")
    validate.add_argument("record_id")
    validate.add_argument("--state", required=True, choices=VALIDATION_STATES)
    validate.add_argument("--note")
    validate.add_argument("--validated-by")
    validate.add_argument("--validated-at")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    associate = subparsers.add_parser("associate", help="Associate two records")
    associate.add_argument("from_record_id")
    associate.add_argument("to_record_id")
    associate.add_argument("--relation", default="associated_with", choices=ASSOCIATION_RELATIONS)
    associate.add_argument("--note")
    associate.add_argument("--strength", type=float)
    associate.add_argument("--source", default="manual", choices=["manual", "agent", "import", "inferred"])
    associate.add_argument("--created-by")
    associate.add_argument("--json", action="store_true")
    associate.set_defaults(func=cmd_associate)

    supersede = subparsers.add_parser("supersede", help="Supersede a record or subject-prefix record set")
    supersede.add_argument("target", help="Old record id, or subject prefix when --before is used")
    supersede.add_argument("replacement", nargs="?", help="Replacement record id")
    supersede.add_argument("--before", help="Bulk supersede records under target subject before this datetime")
    supersede.add_argument("--replacement", dest="replacement_option", help="Replacement record id for --before mode")
    supersede.add_argument("--note")
    supersede.add_argument("--created-by")
    supersede.add_argument("--json", action="store_true")
    supersede.set_defaults(func=cmd_supersede)

    return parser


def add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("subject")
    parser.add_argument("--file", required=True, help="Artifact file to copy into the ledger")
    parser.add_argument("--record-id", help="Attach artifact to an existing record instead of creating a new record")
    parser.add_argument("--label")
    parser.add_argument("--summary")
    parser.add_argument("--body")
    parser.add_argument("--source-uri")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--related-subject", action="append", default=[])
    parser.add_argument("--created-by")
    parser.add_argument("--visibility", default="private", choices=["private", "internal", "shareable", "public"])
    parser.add_argument("--json", action="store_true")


def cmd_init(args: argparse.Namespace, _ledger: EventedLedger, paths: LedgerPaths) -> int:
    paths.events_dir.mkdir(parents=True, exist_ok=True)
    print(f"ledger ready: {paths.home}")
    print(f"events: {paths.events_dir}")
    print(f"sqlite projection: {paths.db_path}")
    return 0


def cmd_rebuild(args: argparse.Namespace, ledger: EventedLedger, paths: LedgerPaths) -> int:
    ledger.rebuild()
    vector_result = None if args.skip_vectors else ledger.rebuild_vectors()
    result = {
        "home": str(paths.home),
        "events_dir": str(paths.events_dir),
        "db_path": str(paths.db_path),
        "skipped_events": ledger.last_rebuild_skipped,
        "vectors": vector_result,
    }
    fallback = f"rebuilt sqlite projection: {paths.db_path}"
    if ledger.last_rebuild_skipped:
        fallback += f" ({len(ledger.last_rebuild_skipped)} events could not be applied; see --json)"
    output(result, args.json, fallback=fallback)
    return 0


def cmd_add(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    body = read_body(args.body, args.body_file)
    record_id = ledger.add_record(
        subject=args.subject,
        kind=args.kind,
        status=args.status,
        summary=args.summary,
        body=body,
        created_by=args.created_by,
        tags=args.tag,
        related_subjects=args.related_subject,
        export_visibility=args.visibility,
        validation_state=args.validation_state,
    )
    output({"id": record_id}, args.json, fallback=f"added {record_id}")
    return 0


def cmd_list(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    rows = [
        dict(row)
        for row in ledger.list_records(
            subject=args.subject,
            kind=args.kind,
            status=args.status,
            exclude_status=args.exclude_status,
            validation_state=args.validation_state,
            include_obsolete=args.all,
            limit=args.limit,
        )
    ]
    output(rows, args.json, fallback=format_rows(rows))
    return 0


def cmd_wip_add(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body
    work_item_id = ledger.add_work_item(
        area=args.area,
        subject=args.subject or f"wip.{args.area}",
        title=args.title,
        body=body,
        expected_by=args.expected_by,
        created_by=args.created_by,
        export_visibility=args.visibility,
    )
    output({"id": work_item_id}, args.json, fallback=f"added {work_item_id}")
    return 0


def cmd_wip_list(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    rows = ledger.list_work_items(
        area=args.area,
        subject=args.subject,
        status=args.status,
        include_closed=args.all,
        expected_from=args.expected_from,
        expected_to=args.expected_to,
        unscheduled=args.unscheduled,
        limit=args.limit,
    )
    output(rows, args.json, fallback=format_work_items(rows))
    return 0


def cmd_wip_show(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    item = ledger.get_work_item(args.work_item_id)
    if item is None:
        raise ValueError(f"work item not found: {args.work_item_id}")
    output(item, args.json, fallback=format_work_item(item))
    return 0


def cmd_wip_update(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    changes: dict[str, Any] = {}
    for field in ("area", "subject", "title", "status"):
        value = getattr(args, field)
        if value is not None:
            changes[field] = value
    if args.body_file:
        changes["body"] = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body is not None:
        changes["body"] = args.body
    if args.clear_expected_by:
        changes["expected_by"] = None
    elif args.expected_by is not None:
        changes["expected_by"] = args.expected_by
    if args.visibility is not None:
        changes["export_visibility"] = args.visibility
    item = ledger.update_work_item(
        work_item_id=args.work_item_id,
        changes=changes,
        updated_by=args.updated_by,
        note=args.note,
    )
    output(item, args.json, fallback=f"updated {args.work_item_id}")
    return 0


def cmd_wip_set_status(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    item = ledger.update_work_item(
        work_item_id=args.work_item_id,
        changes={"status": args.target_status},
        updated_by=args.updated_by,
        note=args.note,
    )
    output(item, args.json, fallback=f"marked {args.work_item_id} {args.target_status}")
    return 0


def cmd_wip_link(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    link_id = ledger.link_work_item_record(
        work_item_id=args.work_item_id,
        record_id=args.record_id,
        relation=args.relation,
        note=args.note,
        created_by=args.created_by,
    )
    output({"id": link_id}, args.json, fallback=f"linked {args.work_item_id} to {args.record_id}")
    return 0


def cmd_topics(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    topics = ledger.list_topics(
        subject=args.subject,
        include_obsolete=args.all,
        direct_only=args.direct,
    )
    output(topics, args.json, fallback=format_topics(topics))
    return 0


def cmd_show(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    record = ledger.get_record(args.record_id)
    if not record:
        raise ValueError(f"record not found: {args.record_id}")
    output(record, args.json, fallback=format_record(record))
    return 0


def cmd_search(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    rows = [
        dict(row)
        for row in ledger.search(
            args.query,
            include_obsolete=args.all,
            validation_state=args.validation_state,
            limit=args.limit,
        )
    ]
    output(rows, args.json, fallback=format_rows(rows))
    return 0


def cmd_vector_search(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    result = ledger.vector_search(
        args.query,
        include_obsolete=args.all,
        validation_state=args.validation_state,
        limit=args.limit,
    )
    output(result, args.json, fallback=format_vector_results(result))
    return 0


def cmd_gather(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    gathered = ledger.gather(args.subject, include_obsolete=args.all)
    output(gathered, args.json, fallback=format_gathered(gathered))
    return 0


def cmd_view(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    view = ledger.subject_view(args.subject, include_obsolete=args.all, limit=args.limit)
    output(view, args.json, fallback=format_view(view))
    return 0


def cmd_coverage(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    report = ledger.coverage_report(
        subject=args.subject,
        include_obsolete=args.all,
        limit=args.limit,
    )
    output(report, args.json, fallback=format_coverage(report))
    return 0


def cmd_evidence_add(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    evidence_id = ledger.add_evidence(
        record_id=args.record_id,
        evidence_type=args.type,
        uri=args.uri,
        line=args.line,
        label=args.label,
        note=args.note,
        observed_at=args.observed_at,
        export_visibility=args.visibility,
        created_by=args.created_by,
    )
    output({"id": evidence_id}, args.json, fallback=f"added {evidence_id}")
    return 0


def cmd_artifact_add_html(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    path = Path(args.file).expanduser()
    content = path.read_bytes()
    result = ledger.add_artifact(
        subject=args.subject,
        artifact_type="html",
        content=content,
        extension=path.suffix or ".html",
        content_type="text/html; charset=utf-8",
        label=args.label or path.name,
        summary=args.summary,
        body=args.body,
        source_uri=args.source_uri or str(path),
        record_id=args.record_id,
        tags=args.tag,
        related_subjects=args.related_subject,
        created_by=args.created_by,
        export_visibility=args.visibility,
    )
    output(result, args.json, fallback=f"added artifact {result['id']} ({result['url']})")
    return 0


def cmd_artifact_add_image(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    path = Path(args.file).expanduser()
    content = path.read_bytes()
    result = ledger.add_artifact(
        subject=args.subject,
        artifact_type="image",
        content=content,
        extension=path.suffix,
        content_type=args.content_type,
        label=args.label or path.name,
        summary=args.summary,
        body=args.body,
        source_uri=args.source_uri or str(path),
        record_id=args.record_id,
        tags=args.tag,
        related_subjects=args.related_subject,
        created_by=args.created_by,
        export_visibility=args.visibility,
    )
    output(result, args.json, fallback=f"added artifact {result['id']} ({result['url']})")
    return 0


def cmd_artifact_add_text(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    if args.file:
        path = Path(args.file).expanduser()
        content = path.read_bytes()
        extension = args.extension or path.suffix or default_text_extension(args.type)
        source_uri = args.source_uri or str(path)
        label = args.label or path.name
    elif args.content is not None:
        content = args.content.encode("utf-8")
        extension = args.extension or default_text_extension(args.type)
        source_uri = args.source_uri
        label = args.label
    elif not sys.stdin.isatty():
        content = sys.stdin.read().encode("utf-8")
        extension = args.extension or default_text_extension(args.type)
        source_uri = args.source_uri
        label = args.label
    else:
        raise ValueError("artifact content is required; use --file, --content, or stdin")
    result = ledger.add_artifact(
        subject=args.subject,
        artifact_type=args.type,
        content=content,
        extension=extension,
        content_type=args.content_type,
        label=label,
        summary=args.summary,
        body=args.body,
        source_uri=source_uri,
        record_id=args.record_id,
        tags=args.tag,
        related_subjects=args.related_subject,
        created_by=args.created_by,
        export_visibility=args.visibility,
    )
    output(result, args.json, fallback=f"added artifact {result['id']} ({result['url']})")
    return 0


def cmd_artifact_associate(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    association_id = ledger.associate_artifact(
        record_id=args.record_id,
        artifact_id=args.artifact_id,
        relation=args.relation,
        note=args.note,
        strength=args.strength,
        source=args.source,
        created_by=args.created_by,
    )
    output({"id": association_id}, args.json, fallback=f"associated artifact {association_id}")
    return 0


def cmd_artifact_list(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    rows = [
        dict(row)
        for row in ledger.list_artifacts(
            subject=args.subject,
            artifact_type=args.type,
            include_obsolete=args.all,
            limit=args.limit,
        )
    ]
    output(rows, args.json, fallback=format_artifacts(rows))
    return 0


def cmd_validate(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    ledger.validate_record(
        record_id=args.record_id,
        validation_state=args.state,
        note=args.note,
        validated_by=args.validated_by,
        validated_at=args.validated_at,
    )
    output({"record_id": args.record_id, "validation_state": args.state}, args.json, fallback=f"validated {args.record_id} as {args.state}")
    return 0


def cmd_associate(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    association_id = ledger.associate(
        from_record_id=args.from_record_id,
        to_record_id=args.to_record_id,
        relation=args.relation,
        note=args.note,
        strength=args.strength,
        source=args.source,
        created_by=args.created_by,
    )
    output({"id": association_id}, args.json, fallback=f"associated {association_id}")
    return 0


def cmd_supersede(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    replacement = args.replacement_option or args.replacement
    if not replacement:
        raise ValueError("replacement record id is required")
    if args.before:
        superseded = ledger.supersede_subject_before(
            subject=args.target,
            before=args.before,
            replacement_record_id=replacement,
            note=args.note,
            created_by=args.created_by,
        )
        output({"superseded": superseded}, args.json, fallback=f"superseded {len(superseded)} records")
    else:
        ledger.supersede_record(
            old_record_id=args.target,
            replacement_record_id=replacement,
            note=args.note,
            created_by=args.created_by,
        )
        output({"superseded": [args.target]}, args.json, fallback=f"superseded {args.target}")
    return 0


def read_body(body: str | None, body_file: str | None) -> str:
    if body is not None:
        return body
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        stdin_body = sys.stdin.read()
        if stdin_body:
            return stdin_body.rstrip("\n")
    raise ValueError("record body is required; use --body, --body-file, or stdin")


def output(value: Any, as_json: bool, *, fallback: str) -> None:
    if as_json:
        print(json_dumps(value))
    else:
        print(fallback)


def format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no records"
    lines = []
    for row in rows:
        summary = f" - {row['summary']}" if row.get("summary") else ""
        lines.append(f"{row['id']} [{row['status']}/{row['kind']}/{row['validation_state']}] {row['subject']}{summary}")
    return "\n".join(lines)


def format_work_items(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no WIP items"
    return "\n".join(
        f"{item['id']} [{item['status']}/{item['area']}] "
        f"expected={item.get('expected_by') or 'unscheduled'} {item['title']}"
        for item in rows
    )


def format_work_item(item: dict[str, Any]) -> str:
    lines = [
        f"{item['id']} [{item['status']}/{item['area']}]",
        f"subject: {item['subject']}",
        f"expected by: {item.get('expected_by') or 'unscheduled'}",
        f"title: {item['title']}",
    ]
    if item.get("body"):
        lines.extend(["", item["body"]])
    if item.get("record_links"):
        lines.append("")
        lines.append("record links:")
        for link in item["record_links"]:
            label = link.get("record_summary") or link["record_id"]
            lines.append(f"  - {link['relation']} -> {link['record_id']} ({label})")
    return "\n".join(lines)


def format_vector_results(result: dict[str, Any]) -> str:
    if result.get("available") is False:
        return f"vector search unavailable: {result.get('error', 'unknown error')}"
    rows = result.get("results") or []
    if not rows:
        return "no records"
    lines = [
        f"vector search: {result.get('model')} dims={result.get('dimensions')} schema={result.get('text_schema')}"
    ]
    for row in rows:
        summary = f" - {row['summary']}" if row.get("summary") else ""
        lines.append(
            f"{row['id']} sim={row['similarity']:.4f} "
            f"[{row['status']}/{row['kind']}/{row['validation_state']}] {row['subject']}{summary}"
        )
    return "\n".join(lines)


def format_topics(topics: list[dict[str, Any]]) -> str:
    if not topics:
        return "no topics"
    lines = []
    for topic in topics:
        indent = "  " * (topic["depth"] - 1)
        lines.append(
            f"{indent}{topic['subject']} "
            f"(direct {topic['direct_records']}, subtree {topic['subtree_records']}, children {topic['child_topics']})"
        )
    return "\n".join(lines)


def format_record(record: dict[str, Any]) -> str:
    lines = [
        f"{record['id']} [{record['status']}/{record['kind']}/{record['validation_state']}]",
        f"subject: {record['subject']}",
    ]
    if record.get("summary"):
        lines.append(f"summary: {record['summary']}")
    lines.extend(
        [
            f"created_at: {record['created_at']}",
            f"validation_state: {record['validation_state']}",
            "",
            record["body"],
        ]
    )
    if record.get("validated_at") or record.get("validated_by") or record.get("validation_note"):
        lines.append("")
        lines.append("validation:")
        if record.get("validated_at"):
            lines.append(f"- at: {record['validated_at']}")
        if record.get("validated_by"):
            lines.append(f"- by: {record['validated_by']}")
        if record.get("validation_note"):
            lines.append(f"- note: {record['validation_note']}")
    if record["tags"]:
        lines.append("")
        lines.append("tags: " + ", ".join(record["tags"]))
    if record["evidence"]:
        lines.append("")
        lines.append("evidence:")
        for item in record["evidence"]:
            label = f" ({item['label']})" if item.get("label") else ""
            lines.append(f"- {item['type']}: {item['uri']}{label}")
    if record.get("artifacts"):
        lines.append("")
        lines.append("artifacts:")
        for item in record["artifacts"]:
            label = f" ({item['label']})" if item.get("label") else ""
            lines.append(f"- {item['type']}: {item['id']}{label} /artifacts/{item['id']}/content")
    if record.get("artifact_associations"):
        lines.append("")
        lines.append("artifact associations:")
        for item in record["artifact_associations"]:
            label = f" ({item['label']})" if item.get("label") else ""
            lines.append(f"- {item['relation']} -> {item['artifact_id']} [{item['artifact_type']}]{label}")
    if record["associations_out"] or record["associations_in"]:
        lines.append("")
        lines.append("associations:")
        for item in record["associations_out"]:
            lines.append(f"- {item['relation']} -> {item['record_id']}")
        for item in record["associations_in"]:
            lines.append(f"- {item['relation']} <- {item['record_id']}")
    return "\n".join(lines)


def format_gathered(gathered: dict[str, Any]) -> str:
    sections = [
        ("current", gathered["current"]),
        ("associated", gathered["associated"]),
        ("obsolete", gathered["obsolete"]),
        ("evidence", gathered["evidence"]),
        ("artifacts", gathered["artifacts"]),
    ]
    lines: list[str] = []
    for title, rows in sections:
        lines.append(f"{title}:")
        if not rows:
            lines.append("  none")
            continue
        for row in rows:
            if "uri" in row:
                lines.append(f"  - {row['record_id']} {row['type']}: {row['uri']}")
            elif "storage_path" in row:
                label = f" - {row['label']}" if row.get("label") else ""
                lines.append(f"  - {row['record_id']} {row['type']}: {row['id']}{label}")
            else:
                summary = f" - {row['summary']}" if row.get("summary") else ""
                relation = f" ({row['relation']})" if row.get("relation") else ""
                lines.append(f"  - {row['id']} [{row['status']}/{row['kind']}/{row['validation_state']}] {row['subject']}{relation}{summary}")
    return "\n".join(lines)


def format_view(view: dict[str, Any]) -> str:
    entries = view["entries"]
    if not entries:
        return "no view entries"
    lines = [f"view: {view['subject']}"]
    for entry in entries:
        if entry["entry_type"] == "artifact":
            label = entry.get("label") or entry.get("summary") or entry["artifact_id"]
            lines.append(
                f"- {entry['created_at']} artifact/{entry['artifact_type']} "
                f"{entry['artifact_id']} record={entry['record_id']} {label}"
            )
        else:
            summary = f" - {entry['summary']}" if entry.get("summary") else ""
            lines.append(
                f"- {entry['created_at']} record/{entry['kind']} "
                f"{entry['record_id']} [{entry['status']}/{entry['validation_state']}] {entry['subject']}{summary}"
            )
    return "\n".join(lines)


def format_artifacts(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no artifacts"
    lines = []
    for row in rows:
        label = f" - {row['label']}" if row.get("label") else ""
        lines.append(
            f"{row['id']} [{row['type']}/{row['content_type']}] "
            f"{row['subject']} record={row['record_id']}{label}"
        )
    return "\n".join(lines)


def format_coverage(report: dict[str, Any]) -> str:
    sections = [
        ("requirements_without_test_cases", report["requirements_without_test_cases"]),
        ("accepted_decisions_without_evidence", report["accepted_decisions_without_evidence"]),
        ("ui_notes_without_artifacts", report["ui_notes_without_artifacts"]),
        ("artifacts_without_associations", report["artifacts_without_associations"]),
    ]
    lines = [f"coverage: {report.get('subject') or '(all)'}"]
    for title, rows in sections:
        lines.append(f"{title}: {len(rows)}")
        for row in rows:
            label = row.get("summary") or row.get("label") or row["id"]
            lines.append(f"  - {row['id']} {row['subject']} - {label}")
    return "\n".join(lines)


def default_text_extension(artifact_type: str) -> str:
    return {
        "snippet": ".txt",
        "pseudocode": ".txt",
        "markdown": ".md",
        "json": ".json",
        "yaml": ".yaml",
        "text": ".txt",
    }[artifact_type]


if __name__ == "__main__":
    raise SystemExit(main())
