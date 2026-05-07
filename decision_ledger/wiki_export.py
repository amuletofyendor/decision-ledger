from __future__ import annotations

import json
import posixpath
import shutil
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from .model import CURRENT_STATUSES, OBSOLETE_STATUSES, now_iso
from .repository import Ledger


PROFILE_VISIBILITY = {
    "internal": {"private", "internal", "shareable", "public"},
    "shareable": {"shareable", "public"},
    "public": {"public"},
}


@dataclass(frozen=True)
class WikiExportResult:
    subject: str
    output_dir: str
    profile: str
    records: int
    subject_pages: int
    record_pages: int
    files: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "output_dir": self.output_dir,
            "profile": self.profile,
            "records": self.records,
            "subject_pages": self.subject_pages,
            "record_pages": self.record_pages,
            "files": self.files,
        }


def export_static_wiki(
    ledger: Ledger,
    *,
    subject: str,
    output_dir: str | Path,
    include_obsolete: bool = False,
    profile: str = "internal",
    clean: bool = False,
) -> WikiExportResult:
    if profile not in PROFILE_VISIBILITY:
        raise ValueError(f"unknown export profile: {profile}")

    out = Path(output_dir).expanduser().resolve()
    if clean and out.exists():
        _validate_clean_target(out)
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "records").mkdir(parents=True, exist_ok=True)
    (out / "subjects").mkdir(parents=True, exist_ok=True)

    records = _records_for_export(
        ledger,
        subject=subject,
        include_obsolete=include_obsolete,
        allowed_visibility=PROFILE_VISIBILITY[profile],
    )
    record_ids = {record["id"] for record in records}
    subject_prefixes = sorted(_subject_prefixes(subject, [record["subject"] for record in records]))

    files: list[Path] = []
    files.append(_write_assets(out))
    files.append(_write_index(out, subject, records, subject_prefixes, profile))

    for prefix in subject_prefixes:
        files.append(_write_subject_page(out, prefix, subject, records, subject_prefixes, profile))

    for record in records:
        files.append(_write_record_page(out, record, record_ids, profile))

    files.append(_write_search_index(out, records))
    files.append(_write_graph(out, records, record_ids))

    rel_files = sorted(str(path.relative_to(out)) for path in files)
    return WikiExportResult(
        subject=subject,
        output_dir=str(out),
        profile=profile,
        records=len(records),
        subject_pages=len(subject_prefixes),
        record_pages=len(records),
        files=rel_files,
    )


def _records_for_export(
    ledger: Ledger,
    *,
    subject: str,
    include_obsolete: bool,
    allowed_visibility: set[str],
) -> list[dict[str, Any]]:
    rows = ledger.list_records(
        subject=subject,
        include_obsolete=include_obsolete,
        limit=10_000,
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        record = ledger.get_record(row["id"])
        if record and record["export_visibility"] in allowed_visibility:
            record["evidence"] = [
                item
                for item in record["evidence"]
                if item["export_visibility"] in allowed_visibility
            ]
            records.append(record)
    return sorted(records, key=lambda item: (item["subject"], item["created_at"], item["id"]))


def _subject_prefixes(root_subject: str, subjects: list[str]) -> set[str]:
    prefixes = {root_subject}
    root_parts = root_subject.split(".")
    for subject in subjects:
        parts = subject.split(".")
        for index in range(len(root_parts), len(parts) + 1):
            prefixes.add(".".join(parts[:index]))
    return prefixes


def _write_assets(out: Path) -> Path:
    css = """
:root {
  color-scheme: light;
  --bg: #f7f7f4;
  --panel: #ffffff;
  --text: #202124;
  --muted: #5f6368;
  --border: #d8d7d0;
  --accent: #226f68;
  --warn: #8a5a00;
  --validated: #226f68;
  --contested: #8f3a22;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
header, main { max-width: 1120px; margin: 0 auto; padding: 24px; }
header { padding-bottom: 8px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { font-size: 28px; line-height: 1.2; margin: 0 0 8px; }
h2 { font-size: 18px; margin: 28px 0 10px; }
h3 { font-size: 15px; margin: 18px 0 8px; }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
pre {
  background: #efeee8;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  overflow: auto;
  white-space: pre-wrap;
}
.meta, .breadcrumb, .empty { color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.card, .record {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px;
}
.record { margin: 10px 0; }
.badge {
  display: inline-block;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 12px;
  color: var(--muted);
  margin-right: 4px;
}
.obsolete .badge.status { color: var(--warn); border-color: #d6b15b; }
.badge.validation.validated { color: var(--validated); border-color: #77aaa4; }
.badge.validation.contested,
.badge.validation.invalidated { color: var(--contested); border-color: #c98773; }
ul.clean { list-style: none; padding: 0; margin: 0; }
ul.clean li { margin: 7px 0; }
.body { max-width: 860px; }
""".strip()
    path = out / "assets" / "styles.css"
    path.write_text(css + "\n", encoding="utf-8")
    return path


def _write_index(out: Path, subject: str, records: list[dict[str, Any]], prefixes: set[str], profile: str) -> Path:
    target = _subject_file(out, subject)
    current = len([record for record in records if record["status"] in CURRENT_STATUSES])
    obsolete = len([record for record in records if record["status"] in OBSOLETE_STATUSES])
    validated = len([record for record in records if record["validation_state"] == "validated"])
    body = [
        f"<p class=\"meta\">Profile: {h(profile)}. Generated: {h(now_iso())}.</p>",
        "<div class=\"grid\">",
        stat_card("Subject root", subject),
        stat_card("Records", str(len(records))),
        stat_card("Current", str(current)),
        stat_card("Obsolete", str(obsolete)),
        stat_card("Validated", str(validated)),
        "</div>",
        "<h2>Start</h2>",
        f"<p><a href=\"{h(_rel(out / 'index.html', target))}\">{h(subject)}</a></p>",
        "<h2>Indexes</h2>",
        "<ul>",
        "<li><a href=\"assets/search-index.json\">Search index JSON</a></li>",
        "<li><a href=\"assets/graph.json\">Graph JSON</a></li>",
        "</ul>",
        "<h2>Subject Pages</h2>",
        render_subject_list(out / "index.html", out, sorted(prefixes)),
    ]
    path = out / "index.html"
    path.write_text(page("Decision Ledger Wiki", body, "assets/styles.css"), encoding="utf-8")
    return path


def _write_subject_page(
    out: Path,
    prefix: str,
    root_subject: str,
    records: list[dict[str, Any]],
    prefixes: set[str],
    profile: str,
) -> Path:
    path = _subject_file(out, prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    direct_children = _direct_child_subjects(prefix, prefixes)
    exact = [record for record in records if record["subject"] == prefix]
    subtree = [record for record in records if record["subject"] == prefix or record["subject"].startswith(prefix + ".")]
    current = [record for record in subtree if record["status"] in CURRENT_STATUSES]
    obsolete = [record for record in subtree if record["status"] in OBSOLETE_STATUSES]

    body = [
        f"<p class=\"breadcrumb\"><a href=\"{h(_rel(path, out / 'index.html'))}\">wiki</a> / {breadcrumb(out, path, prefix)}</p>",
        f"<p class=\"meta\">Root: {h(root_subject)}. Profile: {h(profile)}. Records in subtree: {len(subtree)}.</p>",
    ]
    if direct_children:
        body.extend(["<h2>Child Subjects</h2>", render_subject_list(path, out, direct_children)])
    if exact:
        body.extend(["<h2>Records Exactly Here</h2>", render_record_list(path, out, exact)])
    body.extend(["<h2>Current Records In Subtree</h2>", render_record_list(path, out, current)])
    body.extend(["<h2>Obsolete Records In Subtree</h2>", render_record_list(path, out, obsolete)])

    path.write_text(page(prefix, body, _rel(path, out / "assets" / "styles.css")), encoding="utf-8")
    return path


def _write_record_page(out: Path, record: dict[str, Any], exported_record_ids: set[str], profile: str) -> Path:
    path = _record_file(out, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)

    body = [
        f"<p class=\"breadcrumb\"><a href=\"{h(_rel(path, out / 'index.html'))}\">wiki</a> / "
        f"<a href=\"{h(_rel(path, _subject_file(out, record['subject'])))}\">{h(record['subject'])}</a></p>",
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
    body.extend(["<h2>Associations Out</h2>", render_associations(path, out, record["associations_out"], exported_record_ids, "->")])
    body.extend(["<h2>Associations In</h2>", render_associations(path, out, record["associations_in"], exported_record_ids, "<-")])
    body.extend(["<h2>Events</h2>", render_events(record["events"])])

    path.write_text(page(record.get("summary") or record["id"], body, _rel(path, out / "assets" / "styles.css")), encoding="utf-8")
    return path


def _write_search_index(out: Path, records: list[dict[str, Any]]) -> Path:
    items = [
        {
            "id": record["id"],
            "subject": record["subject"],
            "kind": record["kind"],
            "status": record["status"],
            "validation_state": record["validation_state"],
            "summary": record["summary"],
            "body": record["body"],
            "url": f"../records/{record['id']}/index.html",
        }
        for record in records
    ]
    path = out / "assets" / "search-index.json"
    path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_graph(out: Path, records: list[dict[str, Any]], exported_record_ids: set[str]) -> Path:
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
    path = out / "assets" / "graph.json"
    path.write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render_subject_list(from_file: Path, out: Path, subjects: list[str]) -> str:
    if not subjects:
        return "<p class=\"empty\">none</p>"
    items = [f"<li><a href=\"{h(_rel(from_file, _subject_file(out, subject)))}\">{h(subject)}</a></li>" for subject in subjects]
    return "<ul class=\"clean\">" + "\n".join(items) + "</ul>"


def render_record_list(from_file: Path, out: Path, records: list[dict[str, Any]]) -> str:
    if not records:
        return "<p class=\"empty\">none</p>"
    items = []
    for record in records:
        classes = "record obsolete" if record["status"] in OBSOLETE_STATUSES else "record"
        title = record.get("summary") or record["id"]
        items.append(
            f"<div class=\"{classes}\">"
            f"<div>{badge(record['kind'])}{badge(record['status'], 'status')}{validation_badge(record['validation_state'])}</div>"
            f"<a href=\"{h(_rel(from_file, _record_file(out, record['id'])))}\"><strong>{h(title)}</strong></a>"
            f"<div class=\"meta\">{h(record['subject'])} - {h(record['created_at'])}</div>"
            f"</div>"
        )
    return "\n".join(items)


def render_evidence(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class=\"empty\">none</p>"
    body = ["<ul class=\"clean\">"]
    for item in items:
        label = item.get("label") or item["uri"]
        uri = item["uri"]
        if item["type"] == "url" and (uri.startswith("http://") or uri.startswith("https://")):
            uri_html = f"<a href=\"{h(uri)}\">{h(label)}</a>"
        else:
            uri_html = f"<code>{h(uri)}</code>"
        line = f":{item['line']}" if item.get("line") else ""
        note = f"<div class=\"meta\">{h(item['note'])}</div>" if item.get("note") else ""
        body.append(f"<li>{badge(item['type'])} {uri_html}{h(line)}{note}</li>")
    body.append("</ul>")
    return "\n".join(body)


def render_associations(
    from_file: Path,
    out: Path,
    items: list[dict[str, Any]],
    exported_record_ids: set[str],
    arrow: str,
) -> str:
    if not items:
        return "<p class=\"empty\">none</p>"
    body = ["<ul class=\"clean\">"]
    for item in items:
        record_id = item["record_id"]
        if record_id in exported_record_ids:
            target = f"<a href=\"{h(_rel(from_file, _record_file(out, record_id)))}\">{h(record_id)}</a>"
        else:
            target = f"<code>{h(record_id)}</code>"
        note = f"<div class=\"meta\">{h(item['note'])}</div>" if item.get("note") else ""
        body.append(f"<li>{badge(item['relation'])} {h(arrow)} {target}{note}</li>")
    body.append("</ul>")
    return "\n".join(body)


def render_events(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class=\"empty\">none</p>"
    body = ["<ul class=\"clean\">"]
    for item in items:
        note = f" - {h(item['note'])}" if item.get("note") else ""
        body.append(f"<li>{badge(item['event_type'])} {h(item['event_at'])}{note}</li>")
    body.append("</ul>")
    return "\n".join(body)


def breadcrumb(out: Path, from_file: Path, subject: str) -> str:
    parts = subject.split(".")
    crumbs = []
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        crumbs.append(f"<a href=\"{h(_rel(from_file, _subject_file(out, prefix)))}\">{h(parts[index - 1])}</a>")
    return " / ".join(crumbs)


def stat_card(label: str, value: str) -> str:
    return f"<div class=\"card\"><div class=\"meta\">{h(label)}</div><strong>{h(value)}</strong></div>"


def badge(value: str, extra_class: str = "") -> str:
    class_name = "badge" + (f" {extra_class}" if extra_class else "")
    return f"<span class=\"{class_name}\">{h(value)}</span>"


def validation_badge(value: str) -> str:
    return badge(value, f"validation {value}")


def validation_meta(record: dict[str, Any]) -> str:
    details = []
    if record.get("validated_at"):
        details.append(f"validated at {h(record['validated_at'])}")
    if record.get("validated_by"):
        details.append(f"by {h(record['validated_by'])}")
    if record.get("validation_note"):
        details.append(h(record["validation_note"]))
    if not details:
        return ""
    return f"<p class=\"meta\">Validation: {'; '.join(details)}</p>"


def page(title: str, body: list[str], stylesheet_href: str) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            f"<title>{h(title)} - Decision Ledger</title>",
            f"<link rel=\"stylesheet\" href=\"{h(stylesheet_href)}\">",
            "</head>",
            "<body>",
            "<header>",
            f"<h1>{h(title)}</h1>",
            "</header>",
            "<main>",
            *body,
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _direct_child_subjects(prefix: str, prefixes: set[str]) -> list[str]:
    children = []
    base_len = len(prefix.split("."))
    for candidate in prefixes:
        if candidate.startswith(prefix + ".") and len(candidate.split(".")) == base_len + 1:
            children.append(candidate)
    return sorted(children)


def _validate_clean_target(path: Path) -> None:
    protected = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if path in protected:
        raise ValueError(f"refusing to clean protected output directory: {path}")


def _subject_file(out: Path, subject: str) -> Path:
    return out / "subjects" / Path(*subject.split(".")) / "index.html"


def _record_file(out: Path, record_id: str) -> Path:
    return out / "records" / record_id / "index.html"


def _rel(from_file: Path, to_file: Path) -> str:
    return posixpath.relpath(to_file, from_file.parent)


def h(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)
