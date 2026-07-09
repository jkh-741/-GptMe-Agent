from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from gptme.policyguard.audit import write_policy_event
from gptme.policyguard.evaluator import evaluate_tool_use
from gptme.policyguard.semantic import (
    call_semantic_judge_model,
    parse_semantic_judge_response,
    set_semantic_judge_for_testing,
)
from gptme.policyguard.types import PolicyAction, RiskLevel, SemanticVerdict
from gptme.tools.base import ToolUse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from gptme.message import Message
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


def test_both_mode_runs_thinking_when_fast_is_suspicious(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "both")
    calls: list[str] = []

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        calls.append(stage)
        if stage == "fast":
            return """
            {
              "verdict": "suspicious",
              "action_hint": "ask",
              "risk_level": "medium",
              "confidence": 0.88,
              "requires_thinking": true,
              "reasons": ["Fast judge needs deeper review."]
            }
            """
        return """
        {
          "verdict": "allow",
          "action_hint": "allow",
          "risk_level": "low",
          "confidence": 0.92,
          "requires_thinking": false,
          "reasons": ["Thinking judge cleared the read-only command."]
        }
        """

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert calls == ["fast", "thinking"]
    assert decision.action == PolicyAction.ALLOW
    assert decision.semantic_result is decision.thinking_semantic_result
    assert decision.fast_semantic_result is not None
    assert decision.fast_semantic_result.requires_thinking


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


def test_judge_exception_uses_heuristic_fallback_and_records_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "fast")
    monkeypatch.setenv("GPTME_POLICYGUARD_FAST_MODEL", "mock/error-fast")

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        raise TimeoutError("semantic judge timed out")

    set_semantic_judge_for_testing(judge)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.fast_semantic_result is not None
    assert decision.fast_semantic_result.classifier == "fast"
    assert decision.fast_semantic_result.model == "mock/error-fast"
    assert decision.fast_semantic_result.error is not None
    assert "TimeoutError" in decision.fast_semantic_result.error
    assert "heuristic fallback" in decision.fast_semantic_result.reasons[0]


def test_semantic_judge_response_parses_fenced_json_and_clamps_confidence() -> None:
    result = parse_semantic_judge_response(
        """
        ```json
        {
          "verdict": "block",
          "action_hint": "deny",
          "risk_level": "critical",
          "confidence": 2.5,
          "requires_thinking": false,
          "reasons": "Remote script execution should be blocked."
        }
        ```
        """,
        "thinking",
        "mock/thinking",
    )

    assert result.classifier == "thinking"
    assert result.model == "mock/thinking"
    assert result.verdict == SemanticVerdict.BLOCK
    assert result.action_hint == PolicyAction.DENY
    assert result.risk_level == RiskLevel.CRITICAL
    assert result.confidence == 1.0
    assert result.reasons == ["Remote script execution should be blocked."]


def test_policyguard_audit_event_records_fast_and_thinking_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "both")
    monkeypatch.setenv("GPTME_POLICYGUARD_FAST_MODEL", "mock/fast")
    monkeypatch.setenv("GPTME_POLICYGUARD_THINKING_MODEL", "mock/thinking")

    def judge(stage: str, model: str, request: SemanticRiskRequest) -> str:
        return f"""
        {{
          "verdict": "suspicious",
          "action_hint": "ask",
          "risk_level": "medium",
          "confidence": 0.8,
          "requires_thinking": false,
          "reasons": ["{stage} result"]
        }}
        """

    set_semantic_judge_for_testing(judge)
    normalized, decision = evaluate_tool_use(
        ToolUse("shell", [], "python -c 'print(1)'"),
        workspace=tmp_path,
    )

    write_policy_event(
        logdir=tmp_path / "logs",
        normalized=normalized,
        decision=decision,
        confirmation_result="not-requested",
    )

    event_path = tmp_path / "logs" / "policy-events.jsonl"
    event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])

    assert event["semantic_mode"] == "both"
    assert event["semantic_result"]["classifier"] == "thinking"
    assert event["fast_semantic_result"]["classifier"] == "fast"
    assert event["fast_semantic_result"]["model"] == "mock/fast"
    assert event["thinking_semantic_result"]["classifier"] == "thinking"
    assert event["thinking_semantic_result"]["model"] == "mock/thinking"
    assert event["final_action"] == "ask"


def test_fast_mode_uses_model_judge_when_no_test_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "fast")
    monkeypatch.setenv("GPTME_POLICYGUARD_FAST_MODEL", "deepseek/test-fast")
    captured: dict[str, object] = {}

    def fake_chat_complete(
        messages: list[Message],
        model: str,
        tools: list[object] | None,
        output_schema: type | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> tuple[str, None]:
        captured.update(
            {
                "messages": messages,
                "model": model,
                "tools": tools,
                "output_schema": output_schema,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
            }
        )
        return (
            """
            {
              "verdict": "allow",
              "action_hint": "allow",
              "risk_level": "low",
              "confidence": 0.93,
              "requires_thinking": false,
              "reasons": ["Model judge allowed the read-only command."]
            }
            """,
            None,
        )

    monkeypatch.setattr("gptme.llm.init_llm", lambda provider: None)
    monkeypatch.setattr("gptme.llm._chat_complete", fake_chat_complete)

    _, decision = evaluate_tool_use(
        ToolUse("shell", [], "ls README.md"),
        workspace=tmp_path,
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.fast_semantic_result is not None
    assert decision.fast_semantic_result.classifier == "fast"
    assert decision.fast_semantic_result.model == "deepseek/test-fast"
    assert captured["model"] == "deepseek/test-fast"
    assert captured["tools"] == []
    assert captured["output_schema"] is None
    assert captured["max_tokens"] == 512
    assert captured["temperature"] == 0


def test_model_judge_payload_redacts_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_prompt: dict[str, str] = {}

    def fake_chat_complete(
        messages: list[Message],
        model: str,
        tools: list[object] | None,
        output_schema: type | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> tuple[str, None]:
        captured_prompt["user"] = messages[-1].content
        return (
            """
            {
              "verdict": "suspicious",
              "action_hint": "ask",
              "risk_level": "high",
              "confidence": 0.9,
              "requires_thinking": false,
              "reasons": ["Secret-like value was redacted before review."]
            }
            """,
            None,
        )

    monkeypatch.setattr("gptme.llm.init_llm", lambda provider: None)
    monkeypatch.setattr("gptme.llm._chat_complete", fake_chat_complete)
    raw = 'echo "DEEPSEEK_API_KEY=sk-secretsecretsecret"'
    request = ToolUse("shell", [], raw)
    normalized, _ = evaluate_tool_use(request, workspace=tmp_path)
    semantic_request = normalized.to_semantic_args()

    from gptme.policyguard.types import SemanticRiskRequest

    result = call_semantic_judge_model(
        "fast",
        "deepseek/test-fast",
        SemanticRiskRequest(
            tool_name="shell",
            raw_content=raw,
            normalized_args=semantic_request,
            workspace=tmp_path,
        ),
    )

    assert "sk-secretsecretsecret" not in captured_prompt["user"]
    assert "[REDACTED_SECRET]" in captured_prompt["user"]
    assert '"verdict": "suspicious"' in result
