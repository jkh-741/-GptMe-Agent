from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .types import (
    RiskLevel,
    SemanticRiskRequest,
    SemanticRiskResult,
    SemanticVerdict,
)

SENSITIVE_PATH_RE = re.compile(
    r"(^|[/\\])(\.env|\.netrc|id_rsa|id_dsa|id_ed25519|credentials|secrets?)(\.|[/\\]|$)",
    re.IGNORECASE,
)


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


class FastSemanticClassifier(HeuristicSemanticClassifier):
    name = "fast"


class ThinkingSemanticClassifier(HeuristicSemanticClassifier):
    name = "thinking"


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
