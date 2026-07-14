from __future__ import annotations

import difflib
import os
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

TOMBSTONE_SUFFIX = ".deleted"
_current_overlay: ContextVar[OverlayWorkspace | None] = ContextVar(
    "current_speculation_overlay",
    default=None,
)


def get_current_overlay() -> OverlayWorkspace | None:
    return _current_overlay.get()


@contextmanager
def use_overlay(overlay: OverlayWorkspace) -> Iterator[None]:
    token = _current_overlay.set(overlay)
    try:
        yield
    finally:
        _current_overlay.reset(token)


class OverlayWorkspace:
    """Copy-on-Write view over a real workspace.

    The overlay stores paths under ``overlay_root`` using the same relative path
    layout as the workspace.  A tombstone file marks deletions without touching
    the real workspace.
    """

    def __init__(self, workspace: Path, overlay_root: Path) -> None:
        self.workspace = workspace.resolve()
        self.overlay_root = overlay_root.resolve()
        self.overlay_root.mkdir(parents=True, exist_ok=True)

    def _relative(self, path: Path | str) -> Path:
        raw_path = Path(path)
        abs_path = raw_path if raw_path.is_absolute() else self.workspace / raw_path
        resolved = abs_path.resolve(strict=False)
        try:
            return resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(f"Path is outside workspace: {path}") from exc

    def _overlay_path(self, relative_path: Path) -> Path:
        return self.overlay_root / relative_path

    def _tombstone_path(self, relative_path: Path) -> Path:
        return self.overlay_root / f"{relative_path}{TOMBSTONE_SUFFIX}"

    def read_path(self, path: Path | str) -> Path:
        relative_path = self._relative(path)
        if self._tombstone_path(relative_path).exists():
            raise FileNotFoundError(path)
        overlay_path = self._overlay_path(relative_path)
        if overlay_path.exists():
            return overlay_path
        return self.workspace / relative_path

    def write_path(self, path: Path | str) -> Path:
        relative_path = self._relative(path)
        overlay_path = self._overlay_path(relative_path)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        tombstone_path = self._tombstone_path(relative_path)
        if tombstone_path.exists():
            tombstone_path.unlink()
        return overlay_path

    def materialize_path(self, path: Path | str) -> Path:
        relative_path = self._relative(path)
        overlay_path = self.write_path(relative_path)
        if overlay_path.exists():
            return overlay_path
        real_path = self.workspace / relative_path
        if real_path.exists() and real_path.is_file():
            shutil.copy2(real_path, overlay_path)
        return overlay_path

    def delete_path(self, path: Path | str) -> None:
        relative_path = self._relative(path)
        overlay_path = self._overlay_path(relative_path)
        if overlay_path.exists() and overlay_path.is_file():
            overlay_path.unlink()
        tombstone_path = self._tombstone_path(relative_path)
        tombstone_path.parent.mkdir(parents=True, exist_ok=True)
        tombstone_path.write_text("", encoding="utf-8")

    def listdir(self, path: Path | str = ".") -> list[Path]:
        relative_path = self._relative(path)
        real_dir = self.workspace / relative_path
        overlay_dir = self.overlay_root / relative_path

        names: set[str] = set()
        if real_dir.exists():
            names.update(entry.name for entry in real_dir.iterdir())
        if overlay_dir.exists():
            for entry in overlay_dir.iterdir():
                if entry.name.endswith(TOMBSTONE_SUFFIX):
                    names.discard(entry.name[: -len(TOMBSTONE_SUFFIX)])
                else:
                    names.add(entry.name)
        return [Path(name) for name in sorted(names)]

    def changed_files(self) -> list[Path]:
        changed: list[Path] = []
        for root, _, files in os.walk(self.overlay_root):
            root_path = Path(root)
            for filename in files:
                file_path = root_path / filename
                if filename.endswith(TOMBSTONE_SUFFIX):
                    changed.append(
                        file_path.relative_to(self.overlay_root).with_suffix("")
                    )
                else:
                    changed.append(file_path.relative_to(self.overlay_root))
        return sorted(set(changed))

    def overlay_bytes_written(self) -> int:
        total = 0
        for root, _, files in os.walk(self.overlay_root):
            for filename in files:
                if filename.endswith(TOMBSTONE_SUFFIX):
                    continue
                total += (Path(root) / filename).stat().st_size
        return total

    def diff_summary(self) -> str:
        chunks: list[str] = []
        for relative_path in self.changed_files():
            tombstone_path = self._tombstone_path(relative_path)
            real_path = self.workspace / relative_path
            overlay_path = self.overlay_root / relative_path
            if tombstone_path.exists():
                chunks.append(f"deleted: {relative_path}")
                continue
            old_text = _read_text_or_empty(real_path)
            new_text = _read_text_or_empty(overlay_path)
            chunks.extend(
                difflib.unified_diff(
                    old_text.splitlines(),
                    new_text.splitlines(),
                    fromfile=str(relative_path),
                    tofile=f"overlay/{relative_path}",
                    lineterm="",
                )
            )
        return "\n".join(chunks)


def _read_text_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "<binary file>"
