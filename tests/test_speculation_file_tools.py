from __future__ import annotations

from pathlib import Path

from gptme.speculation.overlay import OverlayWorkspace, use_overlay
from gptme.tools.read import execute_read
from gptme.tools.save import execute_append_impl, execute_save_impl


def test_read_tool_prefers_overlay_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("real\n", encoding="utf-8")
    overlay = OverlayWorkspace(workspace, overlay_root)
    overlay.write_path("notes.txt").write_text("overlay\n", encoding="utf-8")

    with use_overlay(overlay):
        messages = list(execute_read(None, ["notes.txt"], None))

    assert "overlay" in messages[0].content
    assert "real" not in messages[0].content


def test_save_tool_writes_only_to_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("real\n", encoding="utf-8")
    overlay = OverlayWorkspace(workspace, overlay_root)

    with use_overlay(overlay):
        messages = list(execute_save_impl("overlay\n", Path("notes.txt")))

    assert "Saved to notes.txt" in messages[-1].content
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "real\n"
    assert (overlay_root / "notes.txt").read_text(encoding="utf-8") == "overlay\n"


def test_append_tool_materializes_real_file_to_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    overlay_root = tmp_path / "overlay"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("real\n", encoding="utf-8")
    overlay = OverlayWorkspace(workspace, overlay_root)

    with use_overlay(overlay):
        messages = list(execute_append_impl("overlay\n", Path("notes.txt")))

    assert "Appended to notes.txt" in messages[-1].content
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "real\n"
    assert (overlay_root / "notes.txt").read_text(encoding="utf-8") == (
        "real\noverlay\n"
    )
