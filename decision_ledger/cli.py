from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .db import connect
from .event_store import DEFAULT_LEDGER_HOME, EventStore, EventedLedger, LedgerPaths, resolve_ledger_paths
from .model import json_dumps
from .wiki_export import export_static_wiki


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
    rebuild.add_argument("--json", action="store_true")
    rebuild.set_defaults(func=cmd_rebuild)

    add = subparsers.add_parser("add", help="Add a record")
    add.add_argument("subject")
    add.add_argument("--kind", default="thought", choices=["thought", "decision", "assumption", "question", "finding", "plan", "note"])
    add.add_argument("--status", default="active", choices=["active", "proposed", "accepted", "rejected", "superseded", "withdrawn", "resolved", "archived"])
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
    list_cmd.add_argument("--status")
    list_cmd.add_argument("--all", action="store_true", help="Include obsolete records")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

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
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    gather = subparsers.add_parser("gather", help="Gather context for a subject prefix")
    gather.add_argument("subject")
    gather.add_argument("--all", action="store_true", help="Include obsolete records")
    gather.add_argument("--json", action="store_true")
    gather.set_defaults(func=cmd_gather)

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

    associate = subparsers.add_parser("associate", help="Associate two records")
    associate.add_argument("from_record_id")
    associate.add_argument("to_record_id")
    associate.add_argument("--relation", default="associated_with", choices=[
        "associated_with",
        "supersedes",
        "supports",
        "contradicts",
        "depends_on",
        "derived_from",
        "duplicates",
        "clarifies",
        "blocks",
        "implements",
        "raises_question",
        "answers_question",
    ])
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

    wiki = subparsers.add_parser("wiki", help="Export a subject subtree as a static HTML wiki")
    wiki.add_argument("subject")
    wiki.add_argument("--out", required=True, help="Output directory for generated static files")
    wiki.add_argument("--all", action="store_true", help="Include obsolete records")
    wiki.add_argument("--profile", default="internal", choices=["internal", "shareable", "public"])
    wiki.add_argument("--clean", action="store_true", help="Remove the output directory before exporting")
    wiki.add_argument("--json", action="store_true")
    wiki.set_defaults(func=cmd_wiki)

    return parser


def cmd_init(args: argparse.Namespace, _ledger: EventedLedger, paths: LedgerPaths) -> int:
    paths.events_dir.mkdir(parents=True, exist_ok=True)
    print(f"ledger ready: {paths.home}")
    print(f"events: {paths.events_dir}")
    print(f"sqlite projection: {paths.db_path}")
    return 0


def cmd_rebuild(args: argparse.Namespace, ledger: EventedLedger, paths: LedgerPaths) -> int:
    ledger.rebuild()
    result = {
        "home": str(paths.home),
        "events_dir": str(paths.events_dir),
        "db_path": str(paths.db_path),
    }
    output(result, args.json, fallback=f"rebuilt sqlite projection: {paths.db_path}")
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
    )
    output({"id": record_id}, args.json, fallback=f"added {record_id}")
    return 0


def cmd_list(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    rows = [dict(row) for row in ledger.list_records(subject=args.subject, status=args.status, include_obsolete=args.all, limit=args.limit)]
    output(rows, args.json, fallback=format_rows(rows))
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
    rows = [dict(row) for row in ledger.search(args.query, include_obsolete=args.all, limit=args.limit)]
    output(rows, args.json, fallback=format_rows(rows))
    return 0


def cmd_gather(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    gathered = ledger.gather(args.subject, include_obsolete=args.all)
    output(gathered, args.json, fallback=format_gathered(gathered))
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


def cmd_wiki(args: argparse.Namespace, ledger: EventedLedger, _paths: LedgerPaths) -> int:
    result = export_static_wiki(
        ledger,
        subject=args.subject,
        output_dir=args.out,
        include_obsolete=args.all,
        profile=args.profile,
        clean=args.clean,
    ).as_dict()
    output(
        result,
        args.json,
        fallback=(
            f"exported {result['records']} records and {result['subject_pages']} subject pages "
            f"to {result['output_dir']}"
        ),
    )
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
        lines.append(f"{row['id']} [{row['status']}/{row['kind']}] {row['subject']}{summary}")
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
        f"{record['id']} [{record['status']}/{record['kind']}]",
        f"subject: {record['subject']}",
    ]
    if record.get("summary"):
        lines.append(f"summary: {record['summary']}")
    lines.extend(
        [
            f"created_at: {record['created_at']}",
            "",
            record["body"],
        ]
    )
    if record["tags"]:
        lines.append("")
        lines.append("tags: " + ", ".join(record["tags"]))
    if record["evidence"]:
        lines.append("")
        lines.append("evidence:")
        for item in record["evidence"]:
            label = f" ({item['label']})" if item.get("label") else ""
            lines.append(f"- {item['type']}: {item['uri']}{label}")
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
            else:
                summary = f" - {row['summary']}" if row.get("summary") else ""
                relation = f" ({row['relation']})" if row.get("relation") else ""
                lines.append(f"  - {row['id']} [{row['status']}/{row['kind']}] {row['subject']}{relation}{summary}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
