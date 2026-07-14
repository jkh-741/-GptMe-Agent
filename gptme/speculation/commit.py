from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from .overlay import TOMBSTONE_SUFFIX, OverlayWorkspace
from .types import CommitResult, DiscardResult, SpeculationRun

if TYPE_CHECKING:
    from pathlib import Path


def commit_overlay(run: SpeculationRun, workspace: Path) -> CommitResult:
    overlay = OverlayWorkspace(workspace, run.overlay_root)
    committed: list[Path] = []
    skipped: list[Path] = []
    for relative_path in overlay.changed_files():
        tombstone_path = run.overlay_root / f"{relative_path}{TOMBSTONE_SUFFIX}"
        target_path = workspace / relative_path
        if tombstone_path.exists():
            skipped.append(relative_path)
            continue
        source_path = run.overlay_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        committed.append(relative_path)

    return CommitResult(
        run_id=run.run_id,
        committed_files=committed,
        skipped_files=skipped,
        success=True,
        reason="Overlay files copied to workspace.",
    )


def discard_overlay(
    run: SpeculationRun,
    remove_fork_logdir: bool = False,
) -> DiscardResult:
    removed_overlay = False
    removed_fork_logdir = False
    if run.overlay_root.exists():
        shutil.rmtree(run.overlay_root)
        removed_overlay = True
    if remove_fork_logdir and run.fork_logdir.exists():
        shutil.rmtree(run.fork_logdir)
        removed_fork_logdir = True
    return DiscardResult(
        run_id=run.run_id,
        removed_overlay=removed_overlay,
        removed_fork_logdir=removed_fork_logdir,
        reason="Speculation branch discarded.",
    )
