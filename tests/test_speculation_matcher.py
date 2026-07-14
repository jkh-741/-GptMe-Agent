from __future__ import annotations

from pathlib import Path

from gptme.message import Message
from gptme.speculation.matcher import match_score, resolve_speculation
from gptme.speculation.types import (
    PredictedPrompt,
    ResolutionAction,
    SpeculationRun,
    SpeculationStatus,
    SpeculativeToolEvent,
)


def _run(content: str) -> SpeculationRun:
    return SpeculationRun.create(
        prediction=PredictedPrompt.create(content, 0.9, "test", ttl_seconds=120),
        overlay_root=Path("/tmp/speculation/test"),
        fork_logdir=Path("/tmp/log/speculation/test"),
        messages_snapshot_hash="abc",
    )


def test_match_score_uses_keyword_overlap() -> None:
    assert match_score("继续补测试", "继续补充并运行相关测试") > 0
    assert match_score("写 Notion", "运行测试") == 0


def test_resolve_discards_when_no_match() -> None:
    resolution = resolve_speculation(Message("user", "写 Notion"), [_run("运行测试")])

    assert resolution.matched_run_id is None
    assert resolution.action == ResolutionAction.DISCARD_ALL


def test_resolve_reuses_readonly_match() -> None:
    run = _run("继续补测试")
    run.finish(SpeculationStatus.WAITING_CONFIRMATION)

    resolution = resolve_speculation(Message("user", "继续补测试"), [run])

    assert resolution.matched_run_id == run.run_id
    assert resolution.action == ResolutionAction.REUSE_READONLY_RESULT


def test_resolve_asks_user_for_matching_run_with_writes() -> None:
    run = _run("继续补测试")
    run.tool_events.append(SpeculativeToolEvent(tool_name="save", action="write"))
    run.finish(SpeculationStatus.WAITING_CONFIRMATION)

    resolution = resolve_speculation(Message("user", "继续补测试"), [run])

    assert resolution.matched_run_id == run.run_id
    assert resolution.action == ResolutionAction.ASK_USER
