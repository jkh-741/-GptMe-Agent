from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from .types import dumps_jsonl


def write_audit_event(logdir: Path, event: dict[str, Any]) -> None:
    logdir.mkdir(parents=True, exist_ok=True)
    audit_path = logdir / "speculation-events.jsonl"
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(dumps_jsonl(event) + "\n")
