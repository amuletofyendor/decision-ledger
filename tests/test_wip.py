from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import urlopen

import pytest

from decision_ledger.cli import main as cli_main
from decision_ledger.db import connect
from decision_ledger.event_store import EventStore, EventedLedger, resolve_ledger_paths
from decision_ledger.wiki_server import create_server


def make_ledger(tmp_path: Path) -> tuple[EventedLedger, Path]:
    db_path = tmp_path / "ledger.sqlite"
    paths = resolve_ledger_paths(db_path=db_path, cwd=tmp_path)
    return EventedLedger(connect(db_path), EventStore(paths.home), db_path), db_path


def test_wip_quick_capture_schedule_links_close_and_rebuild(tmp_path: Path) -> None:
    ledger, db_path = make_ledger(tmp_path)
    plan_id = ledger.add_record(
        subject="home.office",
        kind="plan",
        status="active",
        summary="Office plan",
        body="Concrete plan and its evidence.",
        export_visibility="internal",
    )
    work_item_id = ledger.add_work_item(
        area="personal",
        subject="home.office",
        title="Finish the office shelves",
        body="Resume from the measured cut list.",
        export_visibility="internal",
    )

    unscheduled = ledger.list_work_items(area="personal", unscheduled=True)
    assert [item["id"] for item in unscheduled] == [work_item_id]
    assert unscheduled[0]["expected_by"] is None

    ledger.update_work_item(
        work_item_id=work_item_id,
        changes={"expected_by": "2026-08-15"},
        updated_by="pytest",
        note="Scheduled during triage.",
    )
    link_id = ledger.link_work_item_record(
        work_item_id=work_item_id,
        record_id=plan_id,
        relation="implements",
        note="The durable plan carries the detail and evidence.",
        created_by="pytest",
    )
    assert link_id.startswith("lnk_")

    item = ledger.get_work_item(work_item_id)
    assert item is not None
    assert item["expected_by"] == "2026-08-15"
    assert item["record_links"][0]["record_id"] == plan_id
    assert item["record_links"][0]["relation"] == "implements"
    assert [event["event_type"] for event in item["events"]] == [
        "created",
        "updated",
        "linked",
    ]

    record = ledger.get_record(plan_id)
    assert record is not None
    assert record["work_items"][0]["work_item_id"] == work_item_id

    ledger.update_work_item(
        work_item_id=work_item_id,
        changes={"status": "closed"},
        updated_by="pytest",
    )
    assert ledger.list_work_items() == []
    assert ledger.list_work_items(status="closed")[0]["closed_at"] is not None

    ledger.rebuild()
    rebuilt = ledger.get_work_item(work_item_id)
    assert rebuilt is not None
    assert rebuilt["status"] == "closed"
    assert rebuilt["expected_by"] == "2026-08-15"
    assert rebuilt["record_links"][0]["record_id"] == plan_id
    assert db_path.exists()

    event = json.loads(
        (tmp_path / "events" / "home" / "office.jsonl").read_text().splitlines()[1]
    )
    assert event["entity_type"] == "work_item"
    assert event["entity_id"] == work_item_id
    assert "record_id" not in event
    ledger.conn.close()


def test_wip_expected_by_validation_is_strict(tmp_path: Path) -> None:
    ledger, _db_path = make_ledger(tmp_path)
    with pytest.raises(ValueError, match="ISO 8601"):
        ledger.add_work_item(
            area="work",
            subject="wip.work",
            title="Invalid schedule",
            expected_by="next Friday",
        )
    with pytest.raises(ValueError, match="timezone"):
        ledger.add_work_item(
            area="work",
            subject="wip.work",
            title="Ambiguous schedule",
            expected_by="2026-08-15T12:30:00",
        )
    ledger.conn.close()


def test_wip_date_bound_includes_datetime_on_the_same_day(tmp_path: Path) -> None:
    ledger, _db_path = make_ledger(tmp_path)
    work_item_id = ledger.add_work_item(
        area="work",
        subject="wip.work",
        title="Afternoon deadline",
        expected_by="2026-08-15T16:00:00+01:00",
    )
    assert [
        item["id"] for item in ledger.list_work_items(expected_to="2026-08-15")
    ] == [work_item_id]
    ledger.conn.close()


def test_cli_wip_quick_capture_schedule_and_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "ledger.sqlite"
    assert (
        cli_main(
            [
                "--db",
                str(db_path),
                "wip",
                "add",
                "Triage the personal inbox",
                "--area",
                "personal",
                "--json",
            ]
        )
        == 0
    )
    work_item_id = json.loads(capsys.readouterr().out)["id"]
    assert (
        cli_main(
            [
                "--db",
                str(db_path),
                "wip",
                "update",
                work_item_id,
                "--expected-by",
                "2026-08-20",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli_main(["--db", str(db_path), "wip", "close", work_item_id, "--json"]) == 0
    closed = json.loads(capsys.readouterr().out)
    assert closed["status"] == "closed"
    assert closed["expected_by"] == "2026-08-20"


def test_live_wiki_has_wip_master_list_and_cross_links(tmp_path: Path) -> None:
    ledger, db_path = make_ledger(tmp_path)
    record_id = ledger.add_record(
        subject="project.release",
        kind="plan",
        status="active",
        summary="Release plan",
        body="Release detail.",
        export_visibility="internal",
    )
    item_id = ledger.add_work_item(
        area="work",
        subject="project.release",
        title="Publish the release",
        expected_by="2026-08-01",
        export_visibility="internal",
    )
    ledger.link_work_item_record(
        work_item_id=item_id,
        record_id=record_id,
        relation="implements",
    )
    ledger.conn.close()

    paths = resolve_ledger_paths(db_path=db_path, cwd=tmp_path)
    server = create_server(
        paths=paths,
        subject="",
        profile="internal",
        include_obsolete=False,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        with urlopen(base_url + "/", timeout=5) as response:  # noqa: S310 - local test server
            index_html = response.read().decode("utf-8")
        assert "WIP master list" in index_html
        assert "Open WIP" in index_html

        with urlopen(base_url + "/wip/index.html", timeout=5) as response:  # noqa: S310
            wip_html = response.read().decode("utf-8")
        assert "Publish the release" in wip_html
        assert "2026-08-01" in wip_html

        with urlopen(base_url + f"/wip/{item_id}/index.html", timeout=5) as response:  # noqa: S310
            item_html = response.read().decode("utf-8")
        assert "Release plan" in item_html

        with urlopen(
            base_url + f"/records/{record_id}/index.html", timeout=5
        ) as response:  # noqa: S310
            record_html = response.read().decode("utf-8")
        assert "WIP Links" in record_html
        assert "Publish the release" in record_html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
