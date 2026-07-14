from __future__ import annotations

import re
from pathlib import Path

from .semantic import SENSITIVE_PATH_RE
from .types import (
    NormalizedToolUse,
    PolicyCheckResult,
    RiskLevel,
    StaticRiskResult,
    max_risk,
)

LARGE_CHANGE_BYTES = 200_000
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
HIGH_RISK_CONFIG_NAMES = {
    "Dockerfile",
    "Makefile",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
}


def check_file_tool(
    tool_use: NormalizedToolUse, workspace: Path | None
) -> StaticRiskResult:
    checks: list[PolicyCheckResult] = []
    resolved_workspace = workspace.resolve() if workspace else Path.cwd().resolve()

    if not tool_use.paths:
        checks.append(
            PolicyCheckResult(
                name="path.present",
                passed=False,
                risk_level=RiskLevel.MEDIUM,
                reason="File-modifying tool did not expose a target path.",
                evidence={"tool": tool_use.tool_name},
            )
        )

    for raw_path in tool_use.paths:
        checks.extend(_check_single_path(raw_path, resolved_workspace))

    content = tool_use.content or tool_use.raw_content
    if len(content.encode("utf-8", errors="ignore")) > LARGE_CHANGE_BYTES:
        checks.append(
            PolicyCheckResult(
                name="path.large_change",
                passed=False,
                risk_level=RiskLevel.MEDIUM,
                reason="Tool call contains a large file-change payload.",
                evidence={"bytes": len(content.encode("utf-8", errors="ignore"))},
            )
        )

    if tool_use.tool_name == "morph":
        checks.append(
            PolicyCheckResult(
                name="path.morph_external_model",
                passed=False,
                risk_level=RiskLevel.HIGH,
                reason="morph sends the target file content to an external model provider.",
                evidence={"paths": [str(path) for path in tool_use.paths]},
            )
        )

    if not checks:
        checks.append(
            PolicyCheckResult(
                name="path.scope",
                passed=True,
                risk_level=RiskLevel.LOW,
                reason="No file path risk found.",
                evidence={},
            )
        )

    failed = [check for check in checks if not check.passed]
    risk = (
        max_risk(*(check.risk_level for check in failed)) if failed else RiskLevel.LOW
    )
    return StaticRiskResult(
        checks=checks,
        risk_level=risk,
        reasons=[check.reason for check in failed],
    )


def _check_single_path(path: Path, workspace: Path) -> list[PolicyCheckResult]:
    checks: list[PolicyCheckResult] = []
    display_path = path.expanduser()
    resolved_path = (
        display_path.resolve()
        if display_path.is_absolute()
        else (workspace / display_path).resolve()
    )
    try:
        resolved_path.relative_to(workspace)
    except ValueError:
        checks.append(
            PolicyCheckResult(
                name="path.workspace_boundary",
                passed=False,
                risk_level=RiskLevel.CRITICAL,
                reason="File-modifying tool targets a path outside the workspace.",
                evidence={
                    "path": str(path),
                    "resolved": str(resolved_path),
                    "workspace": str(workspace),
                },
            )
        )

    path_text = str(path)
    if ".." in path.parts:
        checks.append(
            PolicyCheckResult(
                name="path.traversal",
                passed=False,
                risk_level=RiskLevel.HIGH,
                reason="File path contains parent-directory traversal.",
                evidence={"path": path_text},
            )
        )

    if (
        SENSITIVE_PATH_RE.search(path_text)
        or resolved_path.suffix in SENSITIVE_SUFFIXES
    ):
        checks.append(
            PolicyCheckResult(
                name="path.sensitive_file",
                passed=False,
                risk_level=RiskLevel.HIGH,
                reason="Tool call targets a credential or secret-like file.",
                evidence={"path": path_text},
            )
        )

    normalized_raw_path_text = _normalize_separators(path_text)
    normalized_path_text = _normalize_separators(str(resolved_path))
    if (
        resolved_path.name in HIGH_RISK_CONFIG_NAMES
        or ".github/workflows" in normalized_path_text
        or ".github/workflows" in normalized_raw_path_text
    ):
        checks.append(
            PolicyCheckResult(
                name="path.high_risk_config",
                passed=False,
                risk_level=RiskLevel.MEDIUM,
                reason="Tool call targets build, dependency, CI, or workflow configuration.",
                evidence={"path": path_text},
            )
        )

    return checks


def _normalize_separators(path_text: str) -> str:
    return re.sub(r"/+", "/", path_text.replace("\\", "/"))
