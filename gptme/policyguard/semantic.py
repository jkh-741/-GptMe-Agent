from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .types import (
    PolicyAction,
    RiskLevel,
    SemanticRiskRequest,
    SemanticRiskResult,
    SemanticVerdict,
)

SENSITIVE_PATH_RE = re.compile(
    r"(^|[/\\])(\.env|\.netrc|id_rsa|id_dsa|id_ed25519|credentials|secrets?)(\.|[/\\]|$)",
    re.IGNORECASE,
)

DEFAULT_FAST_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_THINKING_MODEL = "deepseek/deepseek-v4-pro"

SemanticJudge = Callable[[str, str, SemanticRiskRequest], str]
_semantic_judge: SemanticJudge | None = None


class SemanticRiskClassifier(ABC):
    name: str

    @abstractmethod
    def classify(self, request: SemanticRiskRequest) -> SemanticRiskResult:
        raise NotImplementedError


class HeuristicSemanticClassifier(SemanticRiskClassifier):
    name = "heuristic"

    def classify(self, request: SemanticRiskRequest) -> SemanticRiskResult:
        text = " ".join(
            part
            for part in [
                request.raw_content,
                str(request.normalized_args),
                request.recent_user_intent or "",
            ]
            if part
        ).lower()

        if _downloads_and_executes(text):
            return SemanticRiskResult(
                classifier=self.name,
                verdict=SemanticVerdict.BLOCK,
                risk_level=RiskLevel.CRITICAL,
                confidence=0.92,
                reasons=[
                    "Tool call appears to download remote content and execute it."
                ],
                requires_thinking=False,
            )

        if request.tool_name == "morph":
            return SemanticRiskResult(
                classifier=self.name,
                verdict=SemanticVerdict.SUSPICIOUS,
                risk_level=RiskLevel.HIGH,
                confidence=0.85,
                reasons=["morph sends file content to an external model provider."],
                requires_thinking=True,
            )

        paths = [str(path).lower() for path in request.normalized_args.get("paths", [])]
        if any(
            SENSITIVE_PATH_RE.search(path) for path in paths
        ) or SENSITIVE_PATH_RE.search(text):
            return SemanticRiskResult(
                classifier=self.name,
                verdict=SemanticVerdict.SUSPICIOUS,
                risk_level=RiskLevel.HIGH,
                confidence=0.8,
                reasons=["Tool call references credential or secret-like paths."],
                requires_thinking=True,
            )

        if _contains_destructive_intent(text):
            return SemanticRiskResult(
                classifier=self.name,
                verdict=SemanticVerdict.SUSPICIOUS,
                risk_level=RiskLevel.HIGH,
                confidence=0.78,
                reasons=[
                    "Tool call may delete, reset, overwrite, or otherwise mutate local state."
                ],
                requires_thinking=True,
            )

        if request.tool_name in {"save", "append", "patch", "patch_many"}:
            return SemanticRiskResult(
                classifier=self.name,
                verdict=SemanticVerdict.ALLOW,
                risk_level=RiskLevel.LOW,
                confidence=0.7,
                reasons=[
                    "Routine workspace file edit can rely on existing diff/content confirmation."
                ],
                requires_thinking=False,
            )

        return SemanticRiskResult(
            classifier=self.name,
            verdict=SemanticVerdict.ALLOW,
            risk_level=RiskLevel.LOW,
            confidence=0.72,
            reasons=["No obvious semantic risk found by local heuristic rules."],
            requires_thinking=False,
        )


class ModelBackedSemanticClassifier(HeuristicSemanticClassifier):
    model_env_var: str
    default_model: str

    def __init__(self, judge: SemanticJudge | None = None) -> None:
        self._judge = judge

    @property
    def model(self) -> str:
        return os.environ.get(self.model_env_var, self.default_model)

    def classify(self, request: SemanticRiskRequest) -> SemanticRiskResult:
        judge = self._judge or _semantic_judge
        if judge is None:
            return replace(
                super().classify(request),
                classifier=self.name,
                model=self.model,
            )

        try:
            raw = judge(self.name, self.model, request)
            return parse_semantic_judge_response(raw, self.name, self.model)
        except Exception as err:
            return _fallback_result(
                request,
                classifier=self.name,
                model=self.model,
                error=f"{type(err).__name__}: {err}",
            )


class FastSemanticClassifier(ModelBackedSemanticClassifier):
    name = "fast"
    model_env_var = "GPTME_POLICYGUARD_FAST_MODEL"
    default_model = DEFAULT_FAST_MODEL


class ThinkingSemanticClassifier(ModelBackedSemanticClassifier):
    name = "thinking"
    model_env_var = "GPTME_POLICYGUARD_THINKING_MODEL"
    default_model = DEFAULT_THINKING_MODEL


def set_semantic_judge_for_testing(judge: SemanticJudge | None) -> None:
    """Install a process-local judge hook used by tests.

    The production default is None, so this module never calls a real model
    until a later implementation wires in an LLM-backed judge.
    """
    global _semantic_judge
    _semantic_judge = judge


def build_semantic_judge_payload(request: SemanticRiskRequest) -> dict[str, Any]:
    return {
        "tool_name": request.tool_name,
        "raw_content": request.raw_content,
        "normalized_args": request.normalized_args,
        "workspace": str(request.workspace) if request.workspace else None,
        "recent_user_intent": request.recent_user_intent,
        "assistant_plan_or_message": request.assistant_plan_or_message,
        "static_findings_so_far": [
            {
                "name": check.name,
                "passed": check.passed,
                "risk_level": check.risk_level.value,
                "reason": check.reason,
                "evidence": check.evidence,
            }
            for check in request.static_findings_so_far
        ],
    }


def parse_semantic_judge_response(
    raw: str, classifier: str, model: str
) -> SemanticRiskResult:
    data = _loads_json_object(raw)
    verdict = SemanticVerdict(data.get("verdict", SemanticVerdict.SUSPICIOUS.value))
    risk_level = RiskLevel(data.get("risk_level", RiskLevel.MEDIUM.value))
    action_hint_raw = data.get("action_hint")
    action_hint = PolicyAction(action_hint_raw) if action_hint_raw else None
    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(confidence, 1.0))
    reasons = data.get("reasons", [])
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        reasons = ["Semantic judge returned malformed reasons."]

    return SemanticRiskResult(
        classifier=classifier,
        verdict=verdict,
        risk_level=risk_level,
        confidence=confidence,
        reasons=reasons or ["Semantic judge returned no reason."],
        requires_thinking=bool(data.get("requires_thinking", False)),
        action_hint=action_hint,
        model=model,
    )


def _loads_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("semantic judge response must be a JSON object")
    return parsed


def _fallback_result(
    request: SemanticRiskRequest,
    *,
    classifier: str,
    model: str,
    error: str,
) -> SemanticRiskResult:
    heuristic = HeuristicSemanticClassifier().classify(request)
    return replace(
        heuristic,
        classifier=classifier,
        model=model,
        error=error,
        reasons=[
            f"Semantic judge failed; used heuristic fallback ({error}).",
            *heuristic.reasons,
        ],
    )


def _downloads_and_executes(text: str) -> bool:
    return bool(
        re.search(r"\b(curl|wget)\b", text)
        and re.search(r"(\|\s*(bash|sh|python|python3)|\b(sh|bash)\s+-c\b)", text)
    )


def _contains_destructive_intent(text: str) -> bool:
    patterns = [
        r"\brm\s+-[^\n]*r",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\s+-",
        r"\bgit\s+push\s+(-f|--force)\b",
        r"\bshutil\.rmtree\b",
        r"\bPath\([^)]*\)\.unlink\b",
        r"\bdelete\b",
        r"\boverwrite\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)
