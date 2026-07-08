from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class PolicyAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SemanticVerdict(str, Enum):
    ALLOW = "allow"
    SUSPICIOUS = "suspicious"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyCheckResult:
    name: str
    passed: bool
    risk_level: RiskLevel
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StaticRiskResult:
    checks: list[PolicyCheckResult]
    risk_level: RiskLevel
    reasons: list[str]


@dataclass(frozen=True)
class SemanticRiskRequest:
    tool_name: str
    raw_content: str
    normalized_args: dict[str, Any]
    workspace: Path | None = None
    recent_user_intent: str | None = None
    assistant_plan_or_message: str | None = None
    static_findings_so_far: list[PolicyCheckResult] = field(default_factory=list)


@dataclass(frozen=True)
class SemanticRiskResult:
    classifier: str
    verdict: SemanticVerdict
    risk_level: RiskLevel
    confidence: float
    reasons: list[str]
    requires_thinking: bool = False
    action_hint: PolicyAction | None = None
    model: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class NormalizedToolUse:
    tool_name: str
    raw_content: str
    args: list[str]
    kwargs: dict[str, str]
    command: str | None = None
    code: str | None = None
    paths: list[Path] = field(default_factory=list)
    content: str | None = None
    operation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_semantic_args(self) -> dict[str, Any]:
        return {
            "args": self.args,
            "kwargs": self.kwargs,
            "command": self.command,
            "code": self.code,
            "paths": [str(path) for path in self.paths],
            "operation": self.operation,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    risk_level: RiskLevel
    reasons: list[str]
    checks: list[PolicyCheckResult] = field(default_factory=list)
    semantic_result: SemanticRiskResult | None = None
    fast_semantic_result: SemanticRiskResult | None = None
    thinking_semantic_result: SemanticRiskResult | None = None
    static_result: StaticRiskResult | None = None
    requires_explicit_confirmation: bool = False
    semantic_mode: str = "off"


RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda level: RISK_ORDER[level], default=RiskLevel.LOW)
