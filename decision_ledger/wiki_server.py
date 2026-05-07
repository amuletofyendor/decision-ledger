from __future__ import annotations

import argparse
import json
import posixpath
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .db import connect
from .event_store import DEFAULT_LEDGER_HOME, EventStore, EventedLedger, LedgerPaths, resolve_ledger_paths
from .model import CURRENT_STATUSES, OBSOLETE_STATUSES, now_iso
from .repository import Ledger
from .wiki_export import (
    PROFILE_VISIBILITY,
    STYLESHEET_CSS,
    _records_for_export,
    _subject_prefixes,
    badge,
    h,
    page,
    render_evidence,
    render_events,
    subject_tree_roots,
    stat_card,
    validation_badge,
    validation_meta,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_ledger_paths(db_path=args.db, home=args.home)
    ensure_projection(paths)
    server = create_server(
        paths=paths,
        subject=args.subject,
        profile=args.profile,
        include_obsolete=args.all,
        host=args.host,
        port=args.port,
    )
    host, port = server.server_address
    print(f"serving live decision wiki at http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision-wiki-server",
        description="Serve a live Decision Ledger wiki without prebuilding static files.",
    )
    parser.add_argument("subject", nargs="?", default="", help="Subject prefix to serve. Defaults to all subjects.")
    parser.add_argument("--home", help=f"Ledger home containing events/ and ledger.sqlite (default: nearest .decision-ledger or {DEFAULT_LEDGER_HOME})")
    parser.add_argument("--db", help="SQLite projection path (default: <ledger home>/ledger.sqlite)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="Port to bind. Use 0 for a free port.")
    parser.add_argument("--all", action="store_true", help="Include obsolete records")
    parser.add_argument("--profile", default="internal", choices=["internal", "shareable", "public"])
    return parser


def ensure_projection(paths: LedgerPaths) -> None:
    db_existed = paths.db_path.exists()
    conn = connect(paths.db_path)
    ledger = EventedLedger(conn, EventStore(paths.home), paths.db_path)
    try:
        if not db_existed and ledger.event_store.has_events():
            ledger.rebuild()
    finally:
        ledger.conn.close()


def create_server(
    *,
    paths: LedgerPaths,
    subject: str,
    profile: str,
    include_obsolete: bool,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    if profile not in PROFILE_VISIBILITY:
        raise ValueError(f"unknown export profile: {profile}")

    normalized_subject = subject.strip(".")

    class DecisionWikiHandler(BaseHTTPRequestHandler):
        server_version = "DecisionWikiServer/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                body, content_type, status = render_request(
                    paths=paths,
                    root_subject=normalized_subject,
                    profile=profile,
                    include_obsolete=include_obsolete,
                    raw_path=self.path,
                )
            except ValueError as exc:
                body = page("Bad Request", [f"<p>{h(exc)}</p>"], "/assets/styles.css").encode("utf-8")
                content_type = "text/html; charset=utf-8"
                status = HTTPStatus.BAD_REQUEST
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

    return ThreadingHTTPServer((host, port), DecisionWikiHandler)


def render_request(
    *,
    paths: LedgerPaths,
    root_subject: str,
    profile: str,
    include_obsolete: bool,
    raw_path: str,
) -> tuple[bytes, str, HTTPStatus]:
    parsed = urlsplit(raw_path)
    route = normalize_route(parsed.path)

    if route == "/healthz":
        return b'{"status":"ok"}\n', "application/json; charset=utf-8", HTTPStatus.OK
    if route == "/assets/styles.css":
        return stylesheet().encode("utf-8"), "text/css; charset=utf-8", HTTPStatus.OK

    with LiveLedger(paths) as ledger:
        records = records_for_live_view(
            ledger,
            root_subject=root_subject,
            include_obsolete=include_obsolete,
            profile=profile,
        )
        record_ids = {record["id"] for record in records}

        if route in {"/", "/index.html"}:
            html = render_index(root_subject, records, profile)
            return html.encode("utf-8"), "text/html; charset=utf-8", HTTPStatus.OK

        if route == "/assets/search-index.json":
            return json_bytes(search_index(records)), "application/json; charset=utf-8", HTTPStatus.OK

        if route == "/assets/graph.json":
            return json_bytes(graph_index(records, record_ids)), "application/json; charset=utf-8", HTTPStatus.OK

        if route.startswith("/subjects/"):
            subject = subject_from_route(route)
            if subject is None:
                return not_found("Subject page not found")
            html = render_subject_page(root_subject, subject, records, profile)
            return html.encode("utf-8"), "text/html; charset=utf-8", HTTPStatus.OK

        if route.startswith("/records/"):
            record_id = record_id_from_route(route)
            if record_id not in record_ids:
                return not_found("Record not found")
            record = next(record for record in records if record["id"] == record_id)
            html = render_record_page(record, record_ids)
            return html.encode("utf-8"), "text/html; charset=utf-8", HTTPStatus.OK

    return not_found("Wiki page not found")


class LiveLedger:
    def __init__(self, paths: LedgerPaths):
        self.paths = paths
        self.conn = None

    def __enter__(self) -> Ledger:
        self.conn = connect(self.paths.db_path)
        return Ledger(self.conn)

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.conn is not None:
            self.conn.close()


def records_for_live_view(
    ledger: Ledger,
    *,
    root_subject: str,
    include_obsolete: bool,
    profile: str,
) -> list[dict[str, Any]]:
    if root_subject:
        return _records_for_export(
            ledger,
            subject=root_subject,
            include_obsolete=include_obsolete,
            allowed_visibility=PROFILE_VISIBILITY[profile],
        )

    rows = ledger.list_records(include_obsolete=include_obsolete, limit=10_000)
    records: list[dict[str, Any]] = []
    for row in rows:
        record = ledger.get_record(row["id"])
        if record and record["export_visibility"] in PROFILE_VISIBILITY[profile]:
            record["evidence"] = [
                item
                for item in record["evidence"]
                if item["export_visibility"] in PROFILE_VISIBILITY[profile]
            ]
            records.append(record)
    return sorted(records, key=lambda item: (item["subject"], item["created_at"], item["id"]))


def render_index(root_subject: str, records: list[dict[str, Any]], profile: str) -> str:
    subjects = [record["subject"] for record in records]
    prefixes = sorted(_subject_prefixes(root_subject, subjects)) if root_subject else sorted(_all_prefixes(subjects))
    current = len([record for record in records if record["status"] in CURRENT_STATUSES])
    obsolete = len([record for record in records if record["status"] in OBSOLETE_STATUSES])
    validated = len([record for record in records if record["validation_state"] == "validated"])
    title = "Decision Ledger Wiki" if not root_subject else f"Decision Ledger Wiki: {root_subject}"
    body = [
        f"<p class=\"meta\">Live view. Profile: {h(profile)}. Generated: {h(now_iso())}.</p>",
        "<div class=\"grid\">",
        stat_card("Subject root", root_subject or "(all)"),
        stat_card("Records", str(len(records))),
        stat_card("Current", str(current)),
        stat_card("Obsolete", str(obsolete)),
        stat_card("Validated", str(validated)),
        "</div>",
        "<h2>Indexes</h2>",
        "<ul>",
        "<li><a href=\"/assets/search-index.json\">Search index JSON</a></li>",
        "<li><a href=\"/assets/graph.json\">Graph JSON</a></li>",
        "</ul>",
        "<h2>Subject Pages</h2>",
        render_subject_tree(prefixes),
    ]
    return page(title, body, "/assets/styles.css")


def render_subject_page(root_subject: str, subject: str, records: list[dict[str, Any]], profile: str) -> str:
    prefixes = _subject_prefixes(root_subject, [record["subject"] for record in records]) if root_subject else _all_prefixes([record["subject"] for record in records])
    if subject not in prefixes:
        return page("Subject Not Found", [f"<p>No records under <code>{h(subject)}</code>.</p>"], "/assets/styles.css")

    direct_children = direct_child_subjects(subject, prefixes)
    exact = [record for record in records if record["subject"] == subject]
    subtree = [record for record in records if record["subject"] == subject or record["subject"].startswith(subject + ".")]
    current = [record for record in subtree if record["status"] in CURRENT_STATUSES]
    obsolete = [record for record in subtree if record["status"] in OBSOLETE_STATUSES]
    body = [
        f"<p class=\"breadcrumb\"><a href=\"/\">wiki</a> / {breadcrumb(subject)}</p>",
        f"<p class=\"meta\">Root: {h(root_subject or '(all)')}. Profile: {h(profile)}. Records in subtree: {len(subtree)}.</p>",
    ]
    if direct_children:
        body.extend(["<h2>Child Subjects</h2>", render_subject_tree(direct_children)])
    if exact:
        body.extend(["<h2>Records Exactly Here</h2>", render_record_list(exact)])
    body.extend(["<h2>Current Records In Subtree</h2>", render_record_list(current)])
    body.extend(["<h2>Obsolete Records In Subtree</h2>", render_record_list(obsolete)])
    return page(subject, body, "/assets/styles.css")


def render_record_page(record: dict[str, Any], exported_record_ids: set[str]) -> str:
    body = [
        f"<p class=\"breadcrumb\"><a href=\"/\">wiki</a> / "
        f"<a href=\"{h(subject_url(record['subject']))}\">{h(record['subject'])}</a></p>",
        "<div>",
        badge(record["kind"]),
        badge(record["status"], "status"),
        validation_badge(record["validation_state"]),
        badge(record["export_visibility"]),
        "</div>",
    ]
    if record.get("summary"):
        body.append(f"<p><strong>{h(record['summary'])}</strong></p>")
    body.extend(
        [
            f"<p class=\"meta\">Created: {h(record['created_at'])}"
            + (f" by {h(record['created_by'])}" if record.get("created_by") else "")
            + "</p>",
            validation_meta(record),
            f"<pre class=\"body\">{h(record['body'])}</pre>",
        ]
    )
    if record["tags"]:
        body.extend(["<h2>Tags</h2>", "<p>" + " ".join(badge(tag) for tag in record["tags"]) + "</p>"])
    if record["related_subjects"]:
        body.extend(["<h2>Related Subjects</h2>", "<ul class=\"clean\">"])
        for item in record["related_subjects"]:
            body.append(f"<li>{h(item['relation'])}: {h(item['subject'])}</li>")
        body.append("</ul>")
    body.extend(["<h2>Evidence</h2>", render_evidence(record["evidence"])])
    body.extend(["<h2>Associations Out</h2>", render_associations(record["associations_out"], exported_record_ids, "->")])
    body.extend(["<h2>Associations In</h2>", render_associations(record["associations_in"], exported_record_ids, "<-")])
    body.extend(["<h2>Events</h2>", render_events(record["events"])])
    return page(record.get("summary") or record["id"], body, "/assets/styles.css")


def render_subject_list(subjects: list[str]) -> str:
    if not subjects:
        return "<p class=\"empty\">none</p>"
    items = [f"<li><a href=\"{h(subject_url(subject))}\">{h(subject)}</a></li>" for subject in subjects]
    return "<ul class=\"clean\">" + "\n".join(items) + "</ul>"


def render_subject_tree(subjects: list[str]) -> str:
    if not subjects:
        return "<p class=\"empty\">none</p>"
    subject_set = set(subjects)
    return render_subject_tree_nodes(subject_tree_roots(subjects), subject_set)


def render_subject_tree_nodes(subjects: list[str], all_subjects: set[str]) -> str:
    items = []
    for subject in subjects:
        children = direct_child_subjects(subject, all_subjects)
        child_html = render_subject_tree_nodes(children, all_subjects) if children else ""
        items.append(f"<li><a href=\"{h(subject_url(subject))}\">{h(subject)}</a>{child_html}</li>")
    return "<ul class=\"tree\">" + "\n".join(items) + "</ul>"


def render_record_list(records: list[dict[str, Any]]) -> str:
    if not records:
        return "<p class=\"empty\">none</p>"
    items = []
    for record in records:
        classes = "record obsolete" if record["status"] in OBSOLETE_STATUSES else "record"
        title = record.get("summary") or record["id"]
        items.append(
            f"<div class=\"{classes}\">"
            f"<div>{badge(record['kind'])}{badge(record['status'], 'status')}{validation_badge(record['validation_state'])}</div>"
            f"<a href=\"{h(record_url(record['id']))}\"><strong>{h(title)}</strong></a>"
            f"<div class=\"meta\">{h(record['subject'])} - {h(record['created_at'])}</div>"
            f"</div>"
        )
    return "\n".join(items)


def render_associations(items: list[dict[str, Any]], exported_record_ids: set[str], arrow: str) -> str:
    if not items:
        return "<p class=\"empty\">none</p>"
    body = ["<ul class=\"clean\">"]
    for item in items:
        record_id = item["record_id"]
        if record_id in exported_record_ids:
            target = f"<a href=\"{h(record_url(record_id))}\">{h(record_id)}</a>"
        else:
            target = f"<code>{h(record_id)}</code>"
        note = f"<div class=\"meta\">{h(item['note'])}</div>" if item.get("note") else ""
        body.append(f"<li>{badge(item['relation'])} {h(arrow)} {target}{note}</li>")
    body.append("</ul>")
    return "\n".join(body)


def search_index(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": record["id"],
            "subject": record["subject"],
            "kind": record["kind"],
            "status": record["status"],
            "validation_state": record["validation_state"],
            "summary": record["summary"],
            "body": record["body"],
            "url": record_url(record["id"]),
        }
        for record in records
    ]


def graph_index(records: list[dict[str, Any]], exported_record_ids: set[str]) -> dict[str, Any]:
    nodes = [
        {
            "id": record["id"],
            "subject": record["subject"],
            "kind": record["kind"],
            "status": record["status"],
            "validation_state": record["validation_state"],
            "summary": record["summary"],
        }
        for record in records
    ]
    seen_edges: set[tuple[str, str, str]] = set()
    edges = []
    for record in records:
        for item in record["associations_out"]:
            target = item["record_id"]
            key = (record["id"], target, item["relation"])
            if target in exported_record_ids and key not in seen_edges:
                seen_edges.add(key)
                edges.append(
                    {
                        "from": record["id"],
                        "to": target,
                        "relation": item["relation"],
                        "note": item["note"],
                    }
                )
    return {"nodes": nodes, "edges": edges}


def stylesheet() -> str:
    return STYLESHEET_CSS + "\n"


def normalize_route(raw_path: str) -> str:
    decoded = unquote(raw_path)
    normalized = posixpath.normpath("/" + decoded.lstrip("/"))
    if normalized == "/.":
        normalized = "/"
    if normalized == "/.." or normalized.startswith("/../"):
        raise ValueError("path traversal rejected")
    return normalized


def subject_from_route(route: str) -> str | None:
    path = route.removeprefix("/subjects/").removesuffix("/index.html").strip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return None
    return ".".join(parts)


def record_id_from_route(route: str) -> str:
    return route.removeprefix("/records/").removesuffix("/index.html").strip("/")


def _all_prefixes(subjects: list[str]) -> set[str]:
    prefixes: set[str] = set()
    for subject in subjects:
        parts = subject.split(".")
        for index in range(1, len(parts) + 1):
            prefixes.add(".".join(parts[:index]))
    return prefixes


def direct_child_subjects(subject: str, prefixes: set[str]) -> list[str]:
    children = []
    base_len = len(subject.split("."))
    for candidate in prefixes:
        if candidate.startswith(subject + ".") and len(candidate.split(".")) == base_len + 1:
            children.append(candidate)
    return sorted(children)


def breadcrumb(subject: str) -> str:
    parts = subject.split(".")
    crumbs = []
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        crumbs.append(f"<a href=\"{h(subject_url(prefix))}\">{h(parts[index - 1])}</a>")
    return " / ".join(crumbs)


def subject_url(subject: str) -> str:
    return "/subjects/" + "/".join(subject.split(".")) + "/index.html"


def record_url(record_id: str) -> str:
    return f"/records/{record_id}/index.html"


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def not_found(message: str) -> tuple[bytes, str, HTTPStatus]:
    body = page("Not Found", [f"<p>{h(message)}</p>"], "/assets/styles.css")
    return body.encode("utf-8"), "text/html; charset=utf-8", HTTPStatus.NOT_FOUND


if __name__ == "__main__":
    raise SystemExit(main())
