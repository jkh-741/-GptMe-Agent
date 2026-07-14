from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..message import Message
from .path_static import check_file_tool
from .python_static import check_python_code
from .semantic import (
    FastSemanticClassifier,
    HeuristicSemanticClassifier,
    ThinkingSemanticClassifier,
)
from .shell_static import check_shell_command
from .types import (
    RISK_ORDER,
    NormalizedToolUse,
    PolicyAction,
    PolicyCheckResult,
    PolicyDecision,
    RiskLevel,
    SemanticRiskRequest,
    SemanticRiskResult,
    SemanticVerdict,
    StaticRiskResult,
    max_risk,
)

if TYPE_CHECKING:
    from ..logmanager import Log
    from ..tools.base import ToolUse


ENFORCED_TOOLS = {
    "shell",
    "ipython",
    "python",
    "patch",
    "save",
    "append",
    "patch_many",
    "morph",
}


def evaluate_tool_use(
    tool_use: ToolUse,
    *,
    workspace: Path | None = None,
    log: Log | None = None,
) -> tuple[NormalizedToolUse, PolicyDecision]:
    normalized = normalize_tool_use(tool_use)
    if normalized.tool_name not in ENFORCED_TOOLS:
        decision = PolicyDecision(
            action=PolicyAction.ALLOW,
            risk_level=RiskLevel.LOW,
            reasons=["Tool is outside first-version PolicyGuard enforcement scope."],
            semantic_mode=_semantic_mode(),
        )
        return normalized, decision

    static_result = _run_static_checks(normalized, workspace)
    semantic_result, fast_result, thinking_result = _run_semantic_checks(
        normalized, static_result, workspace, log
    )
    decision = merge_policy_results(
        semantic_result,
        static_result,
        fast_semantic_result=fast_result,
        thinking_semantic_result=thinking_result,
    )
    return normalized, decision


def normalize_tool_use(tool_use: ToolUse) -> NormalizedToolUse:
    raw_content = tool_use.content or ""
    args = list(tool_use.args or [])
    kwargs = dict(tool_use.kwargs or {})

    if tool_use.tool == "shell":
        command = raw_content or kwargs.get("command") or kwargs.get("cmd") or ""
        return NormalizedToolUse(
            tool_name=tool_use.tool,
            raw_content=raw_content,
            args=args,
            kwargs=kwargs,
            command=command.strip(),
            operation="execute_shell",
        )

    if tool_use.tool in {"python", "ipython"}:
        code = raw_content or kwargs.get("code") or ""
        return NormalizedToolUse(
            tool_name=tool_use.tool,
            raw_content=raw_content,
            args=args,
            kwargs=kwargs,
            code=code.strip(),
            operation="execute_python",
        )

    if tool_use.tool in {"save", "append", "patch", "morph"}:
        path = _extract_single_path(tool_use.tool, args, kwargs)
        return NormalizedToolUse(
            tool_name=tool_use.tool,
            raw_content=raw_content,
            args=args,
            kwargs=kwargs,
            paths=[path] if path else [],
            content=kwargs.get("content") or kwargs.get("edit") or raw_content,
            operation=tool_use.tool,
        )

    if tool_use.tool == "patch_many":
        return NormalizedToolUse(
            tool_name=tool_use.tool,
            raw_content=raw_content,
            args=args,
            kwargs=kwargs,
            paths=_extract_patch_many_paths(args, kwargs, raw_content),
            content=raw_content or kwargs.get("patches", ""),
            operation="patch_many",
        )

    return NormalizedToolUse(
        tool_name=tool_use.tool,
        raw_content=raw_content,
        args=args,
        kwargs=kwargs,
    )


def merge_policy_results(
    semantic_result: SemanticRiskResult | None,
    static_result: StaticRiskResult | None,
    *,
    fast_semantic_result: SemanticRiskResult | None = None,
    thinking_semantic_result: SemanticRiskResult | None = None,
) -> PolicyDecision:
    static_risk = static_result.risk_level if static_result else RiskLevel.LOW
    semantic_risk = semantic_result.risk_level if semantic_result else RiskLevel.LOW
    risk = max_risk(static_risk, semantic_risk)
    checks = static_result.checks if static_result else []
    reasons = []
    if semantic_result:
        reasons.extend(semantic_result.reasons)
    if static_result:
        reasons.extend(static_result.reasons)
    reasons = _dedupe(reasons) or ["PolicyGuard found no blocking risk."]

    action = PolicyAction.ALLOW
    requires_explicit_confirmation = False

    if (
        (semantic_result and semantic_result.action_hint == PolicyAction.DENY)
        or _has_failed_check(checks, RiskLevel.CRITICAL)
        or (semantic_result and semantic_result.verdict == SemanticVerdict.BLOCK)
    ):
        action = PolicyAction.DENY
    elif semantic_result and semantic_result.action_hint == PolicyAction.ASK:
        action = PolicyAction.ASK
        requires_explicit_confirmation = True
    elif RISK_ORDER[risk] >= RISK_ORDER[RiskLevel.MEDIUM]:
        action = PolicyAction.ASK
        requires_explicit_confirmation = True

    return PolicyDecision(
        action=action,
        risk_level=risk,
        reasons=reasons,
        checks=checks,
        semantic_result=semantic_result,
        fast_semantic_result=fast_semantic_result,
        thinking_semantic_result=thinking_semantic_result,
        static_result=static_result,
        requires_explicit_confirmation=requires_explicit_confirmation,
        semantic_mode=_semantic_mode(),
    )


def policy_preview(decision: PolicyDecision) -> str:
    reasons = "\n".join(f"- {reason}" for reason in decision.reasons)
    return (
        "PolicyGuard requires explicit approval before executing this tool call.\n\n"
        f"Risk level: {decision.risk_level.value}\n"
        f"Reasons:\n{reasons}"
    )


def denied_message(decision: PolicyDecision) -> Message:
    reasons = "\n".join(f"- {reason}" for reason in decision.reasons)
    return Message(
        "system",
        "PolicyGuard blocked this tool call.\n\n"
        f"Risk level: {decision.risk_level.value}\n"
        f"Reasons:\n{reasons}",
    )


def skipped_message(reason: str | None) -> Message:
    return Message(
        "system",
        "PolicyGuard skipped this tool call because explicit approval was not granted."
        + (f"\n\nReason: {reason}" if reason else ""),
    )


def _run_static_checks(
    normalized: NormalizedToolUse, workspace: Path | None
) -> StaticRiskResult:
    if normalized.tool_name == "shell":
        return check_shell_command(normalized.command or "", workspace)
    if normalized.tool_name in {"python", "ipython"}:
        return check_python_code(normalized.code or "")
    if normalized.tool_name in {"patch", "save", "append", "patch_many", "morph"}:
        return check_file_tool(normalized, workspace)
    return StaticRiskResult(checks=[], risk_level=RiskLevel.LOW, reasons=[])


def _run_semantic_checks(
    normalized: NormalizedToolUse,
    static_result: StaticRiskResult,
    workspace: Path | None,
    log: Log | None,
) -> tuple[
    SemanticRiskResult,
    SemanticRiskResult | None,
    SemanticRiskResult | None,
]:
    mode = _semantic_mode()
    request = SemanticRiskRequest(
        tool_name=normalized.tool_name,
        raw_content=normalized.raw_content,
        normalized_args=normalized.to_semantic_args(),
        workspace=workspace,
        recent_user_intent=_recent_message(log, "user"),
        assistant_plan_or_message=_recent_message(log, "assistant"),
        static_findings_so_far=static_result.checks,
    )

    if mode == "off":
        result = HeuristicSemanticClassifier().classify(request)
        return result, None, None
    if mode == "fast":
        fast = FastSemanticClassifier().classify(request)
        return fast, fast, None
    if mode == "thinking":
        thinking = ThinkingSemanticClassifier().classify(request)
        return thinking, None, thinking

    fast = FastSemanticClassifier().classify(request)
    should_think = (
        fast.requires_thinking
        or fast.verdict != SemanticVerdict.ALLOW
        or fast.confidence < 0.7
        or RISK_ORDER[static_result.risk_level] >= RISK_ORDER[RiskLevel.MEDIUM]
    )
    if should_think:
        thinking = ThinkingSemanticClassifier().classify(request)
        return thinking, fast, thinking
    return fast, fast, None


def _semantic_mode() -> str:
    mode = os.environ.get("GPTME_POLICYGUARD_SEMANTIC_MODE", "off").lower()
    return mode if mode in {"off", "fast", "thinking", "both"} else "off"


def _extract_single_path(
    tool_name: str, args: list[str], kwargs: dict[str, str]
) -> Path | None:
    if path := kwargs.get("path"):
        return Path(path).expanduser()
    if args:
        raw = " ".join(args)
        if raw.startswith((f"{tool_name} ", "save ", "append ", "patch ")):
            raw = raw.split(" ", 1)[1]
        return Path(raw).expanduser()
    return None


def _extract_patch_many_paths(
    args: list[str], kwargs: dict[str, str], content: str
) -> list[Path]:
    paths: list[Path] = []
    raw_patches: Any = kwargs.get("patches")
    if isinstance(raw_patches, str):
        try:
            raw_patches = json.loads(raw_patches)
        except json.JSONDecodeError:
            raw_patches = None
    if isinstance(raw_patches, list):
        paths.extend(
            Path(entry["path"]).expanduser()
            for entry in raw_patches
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
    if paths:
        return paths

    header_paths = [Path(arg).expanduser() for arg in args if arg]
    embedded_paths = [
        Path(match.group(1).strip()).expanduser()
        for match in re.finditer(r"(?m)^=== PATH: (.+?) ===$", content)
    ]
    return embedded_paths or header_paths


def _recent_message(log: Log | None, role: str) -> str | None:
    if log is None:
        return None
    for message in reversed(log.messages):
        if message.role == role and message.content:
            return message.content[:4000]
    return None


def _has_failed_check(checks: list[PolicyCheckResult], risk: RiskLevel) -> bool:
    return any(not check.passed and check.risk_level == risk for check in checks)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
