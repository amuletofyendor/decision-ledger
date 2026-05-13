from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any


CURRENT_STATUSES = ("active", "proposed", "accepted", "resolved")
OBSOLETE_STATUSES = ("superseded", "rejected", "withdrawn", "archived")
RECORD_STATUSES = (*CURRENT_STATUSES, *OBSOLETE_STATUSES)
RECORD_KINDS = (
    "thought",
    "idea",
    "snag",
    "decision",
    "assumption",
    "question",
    "finding",
    "plan",
    "note",
    "requirement",
    "constraint",
    "test_case",
    "ui_note",
    "interface_contract",
)
ARTIFACT_TYPES = ("html", "image", "snippet", "pseudocode", "markdown", "json", "yaml", "text")
ASSOCIATION_RELATIONS = (
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
    "verifies",
    "constrains",
    "illustrates",
)
VALIDATION_STATES = ("unvalidated", "partially_validated", "validated", "contested", "invalidated")
SYMMETRIC_RELATIONS = {"associated_with", "duplicates"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def parse_datetime(value: str) -> str:
    normalized = value.strip()
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(normalized).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return value


def rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
