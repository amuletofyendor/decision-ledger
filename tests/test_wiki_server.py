from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import urlopen

from decision_ledger.cli import main as cli_main
from decision_ledger.event_store import resolve_ledger_paths
from decision_ledger.wiki_server import create_server


def run_cli(db_path: Path, *args: str) -> int:
    return cli_main(["--db", str(db_path), *args])


def fetch(base_url: str, path: str) -> str:
    with urlopen(base_url + path, timeout=5) as response:  # noqa: S310 - local test server
        return response.read().decode("utf-8")


def fetch_bytes(base_url: str, path: str) -> tuple[bytes, str]:
    with urlopen(base_url + path, timeout=5) as response:  # noqa: S310 - local test server
        return response.read(), response.headers["Content-Type"]


def test_live_wiki_server_serves_current_ledger_without_static_export(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledger.sqlite"
    html_path = tmp_path / "inline.html"
    html_path.write_text("<!doctype html><script>window.inlineOk=true</script><p>Inline artifact</p>", encoding="utf-8")

    assert run_cli(
        db_path,
        "add",
        "decision-ledger.live.initial",
        "--summary",
        "Initial live record",
        "--body",
        "This record exists before the server starts.",
        "--visibility",
        "internal",
        "--json",
    ) == 0
    first_record_id = json.loads(capsys.readouterr().out)["id"]
    assert run_cli(
        db_path,
        "artifact",
        "add-html",
        "decision-ledger.live.initial",
        "--record-id",
        first_record_id,
        "--file",
        str(html_path),
        "--label",
        "Inline HTML artifact",
        "--visibility",
        "internal",
        "--json",
    ) == 0
    artifact_id = json.loads(capsys.readouterr().out)["id"]

    paths = resolve_ledger_paths(db_path=db_path, cwd=tmp_path)
    server = create_server(
        paths=paths,
        subject="decision-ledger",
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
        index_html = fetch(base_url, "/")
        assert "decision-ledger.live.initial" in index_html
        assert "Live view" in index_html
        assert '<ul class="tree">' in index_html
        assert '<span class="tree-marker" aria-hidden="true">' in index_html
        assert "└─" in index_html or "├─" in index_html

        record_html = fetch(base_url, f"/records/{first_record_id}/index.html")
        assert "This record exists before the server starts." in record_html
        assert f"/artifacts/{artifact_id}/content" in record_html

        view_html = fetch(base_url, "/views/subjects/decision-ledger/live/index.html")
        assert "Initial live record" in view_html
        assert f'<iframe class="artifact-frame" src="/artifacts/{artifact_id}/content"' in view_html

        artifact_body, artifact_type = fetch_bytes(base_url, f"/artifacts/{artifact_id}/content")
        assert b"window.inlineOk=true" in artifact_body
        assert artifact_type.startswith("text/html")

        assert run_cli(
            db_path,
            "add",
            "decision-ledger.live.after-start",
            "--summary",
            "Record added after server start",
            "--body",
            "This proves requests read the current ledger projection.",
            "--visibility",
            "internal",
            "--json",
        ) == 0
        capsys.readouterr()

        search_index = json.loads(fetch(base_url, "/assets/search-index.json"))
        assert "Record added after server start" in {item["summary"] for item in search_index}

        subject_html = fetch(base_url, "/subjects/decision-ledger/live/index.html")
        assert "Record added after server start" in subject_html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
