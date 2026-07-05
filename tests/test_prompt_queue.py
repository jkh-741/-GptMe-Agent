import json
from pathlib import Path

from gptme.message import Message
from gptme.prompt_queue import (
    STATUS_INFLIGHT,
    STATUS_QUEUED,
    ack_prompt_queue_item,
    drain_prompt_queue,
    get_prompt_queue_path,
    queue_prompt,
)


def _read_records(logdir: Path) -> list[dict]:
    queue_path = get_prompt_queue_path(logdir)
    if not queue_path.exists():
        return []
    return [
        json.loads(line)
        for line in queue_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_conversation(logdir: Path, messages: list[Message]) -> None:
    (logdir / "conversation.jsonl").write_text(
        "".join(json.dumps(msg.to_dict()) + "\n" for msg in messages),
        encoding="utf-8",
    )


def test_queue_prompt_writes_durable_record(tmp_path: Path):
    queue_prompt(tmp_path, "follow up")

    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["content"] == "follow up"
    assert records[0]["queue_id"]
    assert records[0]["queued_at"]
    assert records[0]["status"] == STATUS_QUEUED


def test_drain_claims_prompt_without_deleting_it(tmp_path: Path):
    queue_prompt(tmp_path, "follow up")

    messages = drain_prompt_queue(tmp_path)

    assert [msg.content for msg in messages] == ["follow up"]
    assert messages[0].metadata
    assert messages[0].metadata["queue_id"]

    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == STATUS_INFLIGHT
    assert records[0]["queue_id"] == messages[0].metadata["queue_id"]
    assert records[0]["claimed_at"]


def test_ack_removes_appended_prompt(tmp_path: Path):
    queue_prompt(tmp_path, "follow up")
    [msg] = drain_prompt_queue(tmp_path)

    assert msg.metadata
    ack_prompt_queue_item(tmp_path, msg.metadata["queue_id"])

    assert not get_prompt_queue_path(tmp_path).exists()


def test_inflight_without_conversation_entry_is_redelivered(tmp_path: Path):
    queue_prompt(tmp_path, "recover me")
    [first_delivery] = drain_prompt_queue(tmp_path)

    redelivered = drain_prompt_queue(tmp_path)

    assert [msg.content for msg in redelivered] == ["recover me"]
    assert first_delivery.metadata
    assert redelivered[0].metadata
    assert redelivered[0].metadata["queue_id"] == first_delivery.metadata["queue_id"]
    assert _read_records(tmp_path)[0]["status"] == STATUS_INFLIGHT


def test_inflight_with_conversation_entry_is_acked_not_redelivered(tmp_path: Path):
    queue_prompt(tmp_path, "already appended")
    [msg] = drain_prompt_queue(tmp_path)
    _write_conversation(tmp_path, [msg])

    redelivered = drain_prompt_queue(tmp_path)

    assert redelivered == []
    assert not get_prompt_queue_path(tmp_path).exists()


def test_old_queue_record_format_is_claimed_with_generated_queue_id(tmp_path: Path):
    queue_path = get_prompt_queue_path(tmp_path)
    queue_path.write_text(
        json.dumps({"content": "old format", "queued_at": "2026-07-05T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )

    [msg] = drain_prompt_queue(tmp_path)

    assert msg.content == "old format"
    assert msg.metadata
    records = _read_records(tmp_path)
    assert records[0]["queue_id"] == msg.metadata["queue_id"]
    assert records[0]["status"] == STATUS_INFLIGHT


def test_drain_max_items_preserves_fifo_order(tmp_path: Path):
    queue_prompt(tmp_path, "first")
    queue_prompt(tmp_path, "second")

    messages = drain_prompt_queue(tmp_path, max_items=1)

    assert [msg.content for msg in messages] == ["first"]
    records = _read_records(tmp_path)
    assert [record["content"] for record in records] == ["first", "second"]
    assert [record["status"] for record in records] == [
        STATUS_INFLIGHT,
        STATUS_QUEUED,
    ]


def test_malformed_queue_lines_are_skipped(tmp_path: Path):
    queue_path = get_prompt_queue_path(tmp_path)
    queue_path.write_text(
        "not json\n" + json.dumps({"content": "valid"}) + "\n",
        encoding="utf-8",
    )

    messages = drain_prompt_queue(tmp_path)

    assert [msg.content for msg in messages] == ["valid"]
