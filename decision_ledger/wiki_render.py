from __future__ import annotations

from html import escape
from typing import Any

from .model import CURRENT_STATUSES
from .repository import Ledger


PROFILE_VISIBILITY = {
    "internal": {"private", "internal", "shareable", "public"},
    "shareable": {"shareable", "public"},
    "public": {"public"},
}

STYLESHEET_CSS = """
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
ul.tree {
  list-style: none;
  padding-left: 0;
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
}
ul.tree ul.tree { padding-left: 24px; margin-top: 3px; }
ul.tree li { margin: 4px 0; }
.tree-row { display: flex; align-items: baseline; gap: 6px; }
.tree-marker {
  color: var(--muted);
  flex: 0 0 24px;
  text-align: right;
  user-select: none;
}
.tree-subject { font-family: inherit; }
.body { max-width: 860px; }
""".strip()


def records_for_wiki(
    ledger: Ledger,
    *,
    subject: str | None,
    include_obsolete: bool,
    profile: str,
) -> list[dict[str, Any]]:
    if profile not in PROFILE_VISIBILITY:
        raise ValueError(f"unknown wiki profile: {profile}")

    rows = ledger.list_records(
        subject=subject,
        include_obsolete=include_obsolete,
        limit=10_000,
    )
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


def subject_prefixes(root_subject: str, subjects: list[str]) -> set[str]:
    prefixes = {root_subject}
    root_parts = root_subject.split(".")
    for subject in subjects:
        parts = subject.split(".")
        for index in range(len(root_parts), len(parts) + 1):
            prefixes.add(".".join(parts[:index]))
    return prefixes


def all_subject_prefixes(subjects: list[str]) -> set[str]:
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


def subject_tree_roots(subjects: list[str]) -> list[str]:
    subject_set = set(subjects)
    roots = []
    for subject in sorted(subject_set):
        parts = subject.split(".")
        has_parent = any(".".join(parts[:index]) in subject_set for index in range(1, len(parts)))
        if not has_parent:
            roots.append(subject)
    return roots


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


def render_events(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class=\"empty\">none</p>"
    body = ["<ul class=\"clean\">"]
    for item in items:
        note = f" - {h(item['note'])}" if item.get("note") else ""
        body.append(f"<li>{badge(item['event_type'])} {h(item['event_at'])}{note}</li>")
    body.append("</ul>")
    return "\n".join(body)


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


def h(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def current_count(records: list[dict[str, Any]]) -> int:
    return len([record for record in records if record["status"] in CURRENT_STATUSES])
