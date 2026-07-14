from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gptme.logmanager import LogManager
from gptme.policyguard.evaluator import evaluate_tool_use
from gptme.policyguard.python_static import check_python_code
from gptme.policyguard.types import PolicyAction, RiskLevel
from gptme.tools.base import ToolUse

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def init_() -> None:
    """PolicyGuard unit tests need tools, not global LLM initialization."""
    from gptme.tools import _loaded_tools_var
    from gptme.tools.shell import tool as shell_tool

    _loaded_tools_var.set([shell_tool])


def test_shell_allowlisted_command_allows(tmp_path: Path) -> None:
    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.risk_level == RiskLevel.LOW


def test_shell_denylisted_command_denies(tmp_path: Path) -> None:
    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "git reset --hard"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.DENY
    assert decision.risk_level == RiskLevel.CRITICAL


def test_python_ast_detects_alias_subprocess() -> None:
    result = check_python_code(
        """
import subprocess as sp
sp.run(["echo", "hi"])
"""
    )

    assert result.risk_level == RiskLevel.HIGH
    assert any("subprocess.run" in check.reason for check in result.checks)


def test_file_tool_outside_workspace_denies(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    _, decision = evaluate_tool_use(
        ToolUse("save", [str(outside)], "content"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.DENY
    assert decision.risk_level == RiskLevel.CRITICAL


def test_file_tool_relative_workspace_path_allows(tmp_path: Path) -> None:
    _, decision = evaluate_tool_use(
        ToolUse("save", ["notes.txt"], "content"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW


def test_morph_requires_explicit_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    _, decision = evaluate_tool_use(
        ToolUse("morph", [str(target)], "rename function"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ASK
    assert decision.requires_explicit_confirmation


def test_policyguard_ask_is_not_auto_confirmed_without_hook(tmp_path: Path) -> None:
    messages = list(
        ToolUse("shell", [], "python -c 'print(1)'").execute(workspace=tmp_path)
    )

    assert len(messages) == 1
    assert "explicit approval was not granted" in messages[0].content


def test_policyguard_writes_audit_event(tmp_path: Path) -> None:
    logdir = tmp_path / "logs"
    with LogManager(logdir=logdir, lock=False):
        list(ToolUse("shell", [], "ls").execute(workspace=tmp_path))

    event_log = logdir / "policy-events.jsonl"
    assert event_log.exists()
    assert '"tool": "shell"' in event_log.read_text(encoding="utf-8")


def test_windows_style_sensitive_path_is_detected(tmp_path: Path) -> None:
    _, decision = evaluate_tool_use(
        ToolUse("shell", [], r"type C:\Users\me\.env"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ASK
    assert decision.risk_level == RiskLevel.HIGH


@pytest.mark.parametrize(
    "path",
    [
        r".github\workflows\ci.yml",
        r".github\\workflows\\ci.yml",
    ],
)
def test_windows_style_github_workflow_path_is_medium_risk(
    tmp_path: Path, path: str
) -> None:
    _, decision = evaluate_tool_use(
        ToolUse("save", [path], "name: ci"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ASK
    assert decision.risk_level == RiskLevel.MEDIUM
