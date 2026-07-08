from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gptme.policyguard.evaluator import evaluate_tool_use
from gptme.policyguard.semantic import set_semantic_judge_for_testing
from gptme.policyguard.types import PolicyAction
from gptme.tools.base import ToolUse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from gptme.policyguard.types import SemanticRiskRequest


@pytest.fixture(autouse=True)
def reset_semantic_judge() -> Iterator[None]:
    set_semantic_judge_for_testing(None)
    yield
    set_semantic_judge_for_testing(None)


def test_semantic_mode_off_does_not_call_judge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")

    def fail_judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        raise AssertionError("semantic judge should not be called in off mode")

    set_semantic_judge_for_testing(fail_judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.semantic_result
    assert decision.semantic_result.classifier == "heuristic"
    assert decision.fast_semantic_result is None
    assert decision.thinking_semantic_result is None


def test_fast_mode_uses_injected_fast_judge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "fast")
    monkeypatch.setenv("GPTME_POLICYGUARD_FAST_MODEL", "mock/fast")
    calls: list[tuple[str, str]] = []

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        calls.append((stage, model))
        return """
        {
          "verdict": "allow",
          "action_hint": "allow",
          "risk_level": "low",
          "confidence": 0.96,
          "requires_thinking": false,
          "reasons": ["Fast judge found a read-only command."]
        }
        """

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert calls == [("fast", "mock/fast")]
    assert decision.action == PolicyAction.ALLOW
    assert decision.fast_semantic_result
    assert decision.fast_semantic_result.model == "mock/fast"
    assert decision.thinking_semantic_result is None


def test_both_mode_skips_thinking_when_fast_allows_low_static_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "both")
    calls: list[str] = []

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        calls.append(stage)
        return """
        {
          "verdict": "allow",
          "action_hint": "allow",
          "risk_level": "low",
          "confidence": 0.95,
          "requires_thinking": false,
          "reasons": ["Read-only command."]
        }
        """

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert calls == ["fast"]
    assert decision.action == PolicyAction.ALLOW
    assert decision.fast_semantic_result is not None
    assert decision.thinking_semantic_result is None


def test_both_mode_runs_thinking_when_static_risk_is_medium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "both")
    calls: list[str] = []

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        calls.append(stage)
        if stage == "fast":
            return """
            {
              "verdict": "allow",
              "action_hint": "allow",
              "risk_level": "low",
              "confidence": 0.95,
              "requires_thinking": false,
              "reasons": ["Fast judge did not find semantic risk."]
            }
            """
        return """
        {
          "verdict": "suspicious",
          "action_hint": "ask",
          "risk_level": "medium",
          "confidence": 0.84,
          "requires_thinking": false,
          "reasons": ["Unknown shell command should require explicit approval."]
        }
        """

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "python -c 'print(1)'"),
        workspace=tmp_path,
    )

    assert calls == ["fast", "thinking"]
    assert decision.action == PolicyAction.ASK
    assert decision.fast_semantic_result is not None
    assert decision.thinking_semantic_result is not None


def test_thinking_action_hint_deny_blocks_tool_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "thinking")

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        return """
        {
          "verdict": "suspicious",
          "action_hint": "deny",
          "risk_level": "high",
          "confidence": 0.91,
          "requires_thinking": false,
          "reasons": ["Tool call conflicts with the user's stated intent."]
        }
        """

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.DENY
    assert decision.thinking_semantic_result is not None


def test_invalid_judge_json_uses_heuristic_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "fast")

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        return "not json"

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.semantic_result is not None
    assert decision.semantic_result.error is not None
    assert "heuristic fallback" in decision.semantic_result.reasons[0]
