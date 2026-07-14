from __future__ import annotations

from pathlib import Path

import pytest

from gptme.speculation.overlay import OverlayWorkspace


def test_overlay_reads_real_file_until_overlay_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("real\n", encoding="utf-8")

    overlay = OverlayWorkspace(workspace, overlay_root)

    assert overlay.read_path("notes.txt") == workspace / "notes.txt"

    overlay.write_path("notes.txt").write_text("overlay\n", encoding="utf-8")

    assert overlay.read_path("notes.txt").read_text(encoding="utf-8") == "overlay\n"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "real\n"


def test_overlay_write_creates_only_overlay_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()

    overlay = OverlayWorkspace(workspace, overlay_root)
    overlay.write_path("src/app.py").write_text("print('hi')\n", encoding="utf-8")

    assert not (workspace / "src/app.py").exists()
    assert (overlay_root / "src/app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert overlay.changed_files() == [Path("src/app.py")]


def test_overlay_delete_uses_tombstone(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()
    (workspace / "old.txt").write_text("keep real file\n", encoding="utf-8")

    overlay = OverlayWorkspace(workspace, overlay_root)
    overlay.delete_path("old.txt")

    with pytest.raises(FileNotFoundError):
        overlay.read_path("old.txt")
    assert (workspace / "old.txt").exists()
    assert overlay.changed_files() == [Path("old.txt")]


def test_overlay_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()

    overlay = OverlayWorkspace(workspace, overlay_root)

    with pytest.raises(ValueError, match="outside workspace"):
        overlay.write_path(tmp_path / "outside.txt")
