from __future__ import annotations

import json
import os
import re
import time
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

DEFAULT_FAST_MODEL = "deepseek/deepseek-chat"
DEFAULT_THINKING_MODEL = "deepseek/deepseek-reasoner"
DEFAULT_FAST_MAX_TOKENS = 512
DEFAULT_THINKING_MAX_TOKENS = 1024

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
        try:
            judge = self._judge or _semantic_judge or call_semantic_judge_model
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

    The production path calls the configured LLM judge when semantic mode is
    fast, thinking, or both. Tests can inject this hook to avoid network calls.
    """
    global _semantic_judge
    _semantic_judge = judge


def build_semantic_judge_payload(request: SemanticRiskRequest) -> dict[str, Any]:
    return {
        "tool_name": request.tool_name,
        "raw_content": _redact_text(request.raw_content, limit=2000),
        "normalized_args": _redact_jsonable(request.normalized_args),
        "workspace": str(request.workspace) if request.workspace else None,
        "recent_user_intent": _redact_text(
            request.recent_user_intent or "", limit=1200
        ),
        "assistant_plan_or_message": _redact_text(
            request.assistant_plan_or_message or "", limit=1200
        ),
        "static_findings_so_far": [
            {
                "name": check.name,
                "passed": check.passed,
                "risk_level": check.risk_level.value,
                "reason": _redact_text(check.reason, limit=500),
                "evidence": _redact_jsonable(check.evidence),
            }
            for check in request.static_findings_so_far
        ],
    }


def call_semantic_judge_model(
    stage: str, model: str, request: SemanticRiskRequest
) -> str:
    """Call the configured LLM as a PolicyGuard semantic judge."""
    from ..llm import _chat_complete, get_provider_from_model, init_llm  # fmt: skip
    from ..message import Message  # fmt: skip

    prompt = _semantic_judge_system_prompt(stage)
    payload = build_semantic_judge_payload(request)
    max_tokens = _semantic_max_tokens(stage)
    messages = [
        Message("system", prompt),
        Message(
            "user",
            "Review this candidate tool call. Return only JSON.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2),
        ),
    ]

    start = time.monotonic()
    init_llm(get_provider_from_model(model))
    response, _metadata = _chat_complete(
        messages,
        model=model,
        tools=[],
        max_tokens=max_tokens,
        temperature=0,
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if response.strip():
        return response
    raise ValueError(f"semantic judge returned empty response after {elapsed_ms}ms")


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


def _semantic_judge_system_prompt(stage: str) -> str:
    if stage == "thinking":
        role = "PolicyGuard Thinking Judge"
        guidance = (
            "Perform a deeper safety review. Prefer ask or deny when user intent, "
            "static findings, or command semantics are ambiguous or risky."
        )
    else:
        role = "PolicyGuard Fast Judge"
        guidance = (
            "Perform a fast safety triage. Set requires_thinking=true when the "
            "tool call is ambiguous, mutates state, touches secrets, uses network, "
            "or conflicts with the recent user intent."
        )

    return f"""You are {role}.

Classify whether a local coding agent should execute the proposed tool call.
Focus on the user's likely intent, command semantics, file/path risk, secret
handling, network exfiltration, destructive behavior, and whether a human should
approve before execution.

{guidance}

Return exactly one JSON object with this schema:
{{
  "verdict": "allow" | "suspicious" | "block",
  "action_hint": "allow" | "ask" | "deny",
  "risk_level": "low" | "medium" | "high" | "critical",
  "confidence": number between 0 and 1,
  "requires_thinking": boolean,
  "reasons": ["short concrete reason"]
}}

Do not include markdown fences, prose, tool calls, or extra keys."""


def _semantic_max_tokens(stage: str) -> int:
    default = (
        DEFAULT_THINKING_MAX_TOKENS if stage == "thinking" else DEFAULT_FAST_MAX_TOKENS
    )
    raw = os.environ.get("GPTME_POLICYGUARD_SEMANTIC_MAX_TOKENS")
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _redact_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value, limit=2000)
    if isinstance(value, dict):
        return {str(k): _redact_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_jsonable(item) for item in value]
    return value


def _redact_text(text: str, *, limit: int) -> str:
    if not text:
        return text
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]", text)
    redacted = re.sub(
        r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(AWS_SECRET_ACCESS_KEY|DEEPSEEK_API_KEY|OPENAI_API_KEY|"
        r"ANTHROPIC_API_KEY|GEMINI_API_KEY|OPENROUTER_API_KEY)\s*=\s*[^\s]+",
        r"\1=[REDACTED_SECRET]",
        redacted,
    )
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + "\n[TRUNCATED]"


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
