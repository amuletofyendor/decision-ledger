from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_BIN = Path(__file__).resolve().parent.parent / "bin"
LAUNCHERS = ["decisions", "decision-ledger-mcp", "decision-wiki-server"]


def write_fake_python(path: Path, label: str) -> None:
    """Write an executable shell script that reports which interpreter ran."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def install_launcher(root: Path, name: str) -> Path:
    """Copy a repo launcher into a scratch repo layout."""
    target = root / "bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_BIN / name, target)
    return target


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_prefers_repo_venv(tmp_path: Path, name: str) -> None:
    launcher = install_launcher(tmp_path, name)
    write_fake_python(tmp_path / ".venv" / "bin" / "python", "venv-python")

    completed = subprocess.run([str(launcher), "--flag"], capture_output=True, text=True, check=True)

    assert completed.stdout.split() == ["venv-python", str(launcher), "--flag"]


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_falls_back_to_python3(tmp_path: Path, name: str) -> None:
    launcher = install_launcher(tmp_path, name)
    fake_bin = tmp_path / "fake-path"
    write_fake_python(fake_bin / "python3", "system-python3")
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    completed = subprocess.run([str(launcher)], capture_output=True, text=True, check=True, env=env)

    assert completed.stdout.split() == ["system-python3", str(launcher)]


def test_launcher_runs_python_entry_point_via_symlink(tmp_path: Path) -> None:
    link = tmp_path / "decisions"
    link.symlink_to(REPO_BIN / "decisions")

    completed = subprocess.run([str(link), "--help"], capture_output=True, text=True, check=True)

    assert "usage: decisions" in completed.stdout
