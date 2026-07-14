from __future__ import annotations

from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.speculation.fork_agent import run_fork_once
from gptme.speculation.types import (
    PredictedPrompt,
    SpeculationContext,
    SpeculationRun,
    SpeculationStatus,
)
from gptme.tools import init_tools

if TYPE_CHECKING:
    from pathlib import Path

    from gptme.tools import ToolUse


def _context(tmp_path: Path) -> SpeculationContext:
    workspace = tmp_path / "workspace"
    logdir = tmp_path / "log"
    workspace.mkdir()
    logdir.mkdir()
    messages = [
        Message("user", "继续"),
        Message("assistant", "好的"),
    ]
    return SpeculationContext(
        conversation_id="test",
        logdir=logdir,
        workspace=workspace,
        messages_snapshot=messages,
        last_assistant_message=messages[-1],
        completed_turns=3,
    )


def _run(tmp_path: Path) -> SpeculationRun:
    return SpeculationRun.create(
        prediction=PredictedPrompt.create("继续", 0.9, "test", ttl_seconds=120),
        overlay_root=tmp_path / "overlay",
        fork_logdir=tmp_path / "log" / "speculation" / "run",
        messages_snapshot_hash="hash",
    )


def test_fork_executes_injected_executor_only_after_policy_allow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["shell"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    calls: list[str] = []

    def executor(tool_use: ToolUse) -> list[Message]:
        calls.append(tool_use.tool)
        return [Message("system", "listed")]

    run = run_fork_once(
        _context(tmp_path),
        _run(tmp_path),
        assistant_message=Message("assistant", "```shell\nls\n```"),
        tool_executor=executor,
    )

    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert calls == ["shell"]
    assert run.result_messages == [Message("system", "listed")]
    assert run.tool_events[0].action == "execute"


def test_fork_pauses_on_policy_ask(tmp_path: Path, monkeypatch) -> None:
    init_tools(allowlist=["shell"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    calls: list[str] = []

    def executor(tool_use: ToolUse) -> list[Message]:
        calls.append(tool_use.tool)
        return [Message("system", "should not run")]

    run = run_fork_once(
        _context(tmp_path),
        _run(tmp_path),
        assistant_message=Message("assistant", "```shell\nrm notes.txt\n```"),
        tool_executor=executor,
    )

    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert calls == []
    assert run.result_messages == []
    assert run.tool_events[0].action == "ask"


def test_fork_blocks_on_policy_deny(tmp_path: Path, monkeypatch) -> None:
    init_tools(allowlist=["shell"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")

    run = run_fork_once(
        _context(tmp_path),
        _run(tmp_path),
        assistant_message=Message("assistant", "```shell\ngit reset --hard\n```"),
        tool_executor=lambda _tool_use: [Message("system", "should not run")],
    )

    assert run.status == SpeculationStatus.BLOCKED
    assert run.result_messages == []
    assert run.tool_events[0].action == "deny"


def test_fork_default_executor_reads_through_overlay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["read"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    context = _context(tmp_path)
    (context.workspace / "notes.txt").write_text("hello\n", encoding="utf-8")

    run = run_fork_once(
        context,
        _run(tmp_path),
        assistant_message=Message("assistant", "```read notes.txt\n```"),
    )

    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert "hello" in run.result_messages[0].content
    assert run.tool_events[0].action == "execute"


def test_fork_default_executor_pauses_writes_unless_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["save"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    context = _context(tmp_path)
    run = _run(tmp_path)

    run = run_fork_once(
        context,
        run,
        assistant_message=Message("assistant", "```save notes.txt\nhello\n```"),
    )

    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert run.result_messages == []
    assert run.tool_events[0].action == "ask"
    assert not (context.workspace / "notes.txt").exists()
    assert not (run.overlay_root / "notes.txt").exists()


def test_fork_default_executor_writes_to_overlay_when_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["save"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    context = _context(tmp_path)
    run = _run(tmp_path)

    run = run_fork_once(
        context,
        run,
        assistant_message=Message("assistant", "```save notes.txt\nhello\n```"),
        allow_writes=True,
    )

    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert run.tool_events[0].action == "write"
    assert not (context.workspace / "notes.txt").exists()
    assert (run.overlay_root / "notes.txt").read_text(encoding="utf-8") == "hello\n"
