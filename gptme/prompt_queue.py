"""Durable queued prompts for active conversations."""

from __future__ import annotations

import importlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .message import Message

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover
    fcntl = None


QUEUE_FILENAME = "prompt-queue.jsonl"
LOCK_FILENAME = ".prompt-queue.lock"
STATUS_QUEUED = "queued"
STATUS_INFLIGHT = "inflight"


@dataclass
class _QueueRecord:
    queue_id: str
    content: str
    queued_at: str
    status: str
    claimed_at: str | None = None

    def to_json(self) -> str:
        data = {
            "queue_id": self.queue_id,
            "content": self.content,
            "queued_at": self.queued_at,
            "status": self.status,
        }
        if self.claimed_at:
            data["claimed_at"] = self.claimed_at
        return json.dumps(data)


def get_prompt_queue_path(logdir: Path) -> Path:
    return logdir / QUEUE_FILENAME


def _get_prompt_queue_lock_path(logdir: Path) -> Path:
    return logdir / LOCK_FILENAME


@contextmanager
def _prompt_queue_lock(logdir: Path):
    lock_path = _get_prompt_queue_lock_path(logdir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+") as fd:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)


def queue_prompt(logdir: Path, content: str) -> None:
    """Append a prompt to a conversation queue."""
    queue_path = get_prompt_queue_path(logdir)
    record = _QueueRecord(
        queue_id=str(uuid4()),
        content=content,
        queued_at=datetime.now(timezone.utc).isoformat(),
        status=STATUS_QUEUED,
    )

    with _prompt_queue_lock(logdir), queue_path.open("a", encoding="utf-8") as f:
        f.write(record.to_json() + "\n")


def drain_prompt_queue(
    logdir: Path,
    max_items: int | None = None,
    exclude_queue_ids: set[str] | None = None,
) -> list[Message]:
    """Claim queued prompts into in-memory Message objects.

    If ``max_items`` is set, any extra prompts remain on disk in FIFO order.
    ``exclude_queue_ids`` protects prompts already claimed into the current
    process's in-memory queue from being redelivered before they are appended.
    Claimed prompts stay on disk as ``inflight`` until the chat loop appends
    them to ``conversation.jsonl`` and calls :func:`ack_prompt_queue_item`.
    """
    queue_path = get_prompt_queue_path(logdir)
    if not queue_path.exists():
        return []

    with _prompt_queue_lock(logdir):
        if not queue_path.exists():
            return []

        appended_queue_ids = _read_appended_queue_ids(logdir)
        exclude_queue_ids = exclude_queue_ids or set()
        records = _read_queue_records(queue_path)
        drained: list[Message] = []
        remaining: list[_QueueRecord] = []
        claimed_at = datetime.now(timezone.utc).isoformat()

        for record in records:
            if record.queue_id in appended_queue_ids:
                continue

            if record.queue_id in exclude_queue_ids:
                remaining.append(record)
                continue

            if record.status not in {STATUS_QUEUED, STATUS_INFLIGHT}:
                logger.warning(
                    "Keeping queued prompt with unknown status %r in %s",
                    record.status,
                    queue_path,
                )
                remaining.append(record)
                continue

            if max_items is not None and len(drained) >= max_items:
                remaining.append(record)
                continue

            record.status = STATUS_INFLIGHT
            record.claimed_at = claimed_at
            remaining.append(record)
            drained.append(
                Message(
                    "user",
                    record.content,
                    quiet=True,
                    metadata={"queue_id": record.queue_id},
                )
            )

        _write_queue_records(queue_path, remaining)

        return drained


def ack_prompt_queue_item(logdir: Path, queue_id: str | None) -> None:
    """Remove a queued prompt after it has been safely appended to the log."""
    if not queue_id:
        return

    queue_path = get_prompt_queue_path(logdir)
    if not queue_path.exists():
        return

    with _prompt_queue_lock(logdir):
        if not queue_path.exists():
            return
        records = [
            record
            for record in _read_queue_records(queue_path)
            if record.queue_id != queue_id
        ]
        _write_queue_records(queue_path, records)


def get_message_queue_id(msg: Message) -> str | None:
    """Return the durable queue identifier stored on a message, if any."""
    if not msg.metadata:
        return None
    queue_id = msg.metadata.get("queue_id")
    return queue_id if isinstance(queue_id, str) and queue_id else None


def _read_queue_records(queue_path: Path) -> list[_QueueRecord]:
    records: list[_QueueRecord] = []
    for line in queue_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed queued prompt in %s", queue_path)
            continue
        record = _normalize_queue_record(raw, queue_path)
        if record:
            records.append(record)
    return records


def _normalize_queue_record(raw: object, queue_path: Path) -> _QueueRecord | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-object queued prompt in %s", queue_path)
        return None

    content = str(raw.get("content", "")).strip()
    if not content:
        logger.warning("Skipping empty queued prompt in %s", queue_path)
        return None

    queue_id = str(raw.get("queue_id") or uuid4())
    queued_at = str(raw.get("queued_at") or datetime.now(timezone.utc).isoformat())
    status = str(raw.get("status") or STATUS_QUEUED)
    claimed_at_raw = raw.get("claimed_at")
    claimed_at = str(claimed_at_raw) if claimed_at_raw else None

    return _QueueRecord(
        queue_id=queue_id,
        content=content,
        queued_at=queued_at,
        status=status,
        claimed_at=claimed_at,
    )


def _write_queue_records(queue_path: Path, records: list[_QueueRecord]) -> None:
    if records:
        queue_path.write_text(
            "\n".join(record.to_json() for record in records) + "\n",
            encoding="utf-8",
        )
    else:
        queue_path.unlink(missing_ok=True)


def _read_appended_queue_ids(logdir: Path) -> set[str]:
    conversation_path = logdir / "conversation.jsonl"
    if not conversation_path.exists():
        return set()

    queue_ids: set[str] = set()
    for line in conversation_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping malformed conversation message in %s", conversation_path
            )
            continue
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            continue
        queue_id = metadata.get("queue_id")
        if isinstance(queue_id, str) and queue_id:
            queue_ids.add(queue_id)
    return queue_ids
