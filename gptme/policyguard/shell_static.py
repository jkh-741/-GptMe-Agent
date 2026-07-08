from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from ..tools.shell_validation import (
    check_with_shellcheck,
    is_allowlisted,
    is_denylisted,
)
from .semantic import SENSITIVE_PATH_RE
from .types import PolicyCheckResult, RiskLevel, StaticRiskResult, max_risk


def check_shell_command(
    command: str, workspace: Path | None = None
) -> StaticRiskResult:
    checks: list[PolicyCheckResult] = []

    denied, reason, matched = is_denylisted(command)
    checks.append(
        PolicyCheckResult(
            name="shell.denylist",
            passed=not denied,
            risk_level=RiskLevel.CRITICAL if denied else RiskLevel.LOW,
            reason=reason or "Command did not match shell denylist.",
            evidence={"matched": matched},
        )
    )

    allowed = is_allowlisted(command)
    checks.append(
        PolicyCheckResult(
            name="shell.allowlist",
            passed=allowed,
            risk_level=RiskLevel.LOW if allowed else RiskLevel.MEDIUM,
            reason=(
                "Command is allowlisted and has no output redirection."
                if allowed
                else "Command is not fully allowlisted."
            ),
            evidence={"command": command},
        )
    )

    has_issues, should_block, message = check_with_shellcheck(command)
    if has_issues:
        checks.append(
            PolicyCheckResult(
                name="shell.shellcheck",
                passed=not should_block,
                risk_level=RiskLevel.HIGH if should_block else RiskLevel.MEDIUM,
                reason=message,
                evidence={"should_block": should_block},
            )
        )

    checks.extend(_check_sensitive_paths(command))
    checks.extend(_check_workspace_paths(command, workspace))
    checks.extend(_check_mutation_patterns(command))

    return _static_result(checks)


def _check_sensitive_paths(command: str) -> list[PolicyCheckResult]:
    if SENSITIVE_PATH_RE.search(command):
        return [
            PolicyCheckResult(
                name="shell.sensitive_path",
                passed=False,
                risk_level=RiskLevel.HIGH,
                reason="Command references credential or secret-like paths.",
                evidence={"command": command},
            )
        ]
    return []


def _check_workspace_paths(
    command: str, workspace: Path | None
) -> list[PolicyCheckResult]:
    if workspace is None:
        return []

    checks: list[PolicyCheckResult] = []
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return checks

    resolved_workspace = workspace.resolve()
    for token in tokens:
        if token.startswith("-") or not _looks_like_path(token):
            continue
        path = Path(token).expanduser()
        if not path.is_absolute():
            continue
        try:
            path.resolve().relative_to(resolved_workspace)
        except (OSError, ValueError):
            checks.append(
                PolicyCheckResult(
                    name="shell.workspace_path",
                    passed=False,
                    risk_level=RiskLevel.HIGH,
                    reason="Command references an absolute path outside the workspace.",
                    evidence={"path": token, "workspace": str(resolved_workspace)},
                )
            )
    return checks


def _check_mutation_patterns(command: str) -> list[PolicyCheckResult]:
    patterns = [
        (r"\brm\b", RiskLevel.HIGH, "Command may remove files."),
        (r"\bmv\b", RiskLevel.MEDIUM, "Command may move or overwrite files."),
        (
            r"\bchmod\b|\bchown\b",
            RiskLevel.MEDIUM,
            "Command may change permissions or ownership.",
        ),
        (r">\s*[^&]|\btee\b", RiskLevel.MEDIUM, "Command may write to files."),
        (r"\b(curl|wget)\b", RiskLevel.MEDIUM, "Command performs network download."),
    ]
    checks: list[PolicyCheckResult] = []
    for pattern, risk, reason in patterns:
        if re.search(pattern, command):
            checks.append(
                PolicyCheckResult(
                    name="shell.mutation_pattern",
                    passed=False,
                    risk_level=risk,
                    reason=reason,
                    evidence={"pattern": pattern},
                )
            )
    return checks


def _looks_like_path(token: str) -> bool:
    return bool(
        token.startswith(("/", "\\", "~", "../", "./", "..\\", ".\\"))
        or "/" in token
        or "\\" in token
        or re.match(r"^[A-Za-z]:[\\/]", token)
    )


def _static_result(checks: list[PolicyCheckResult]) -> StaticRiskResult:
    failed = [check for check in checks if not check.passed]
    risk = (
        max_risk(*(check.risk_level for check in failed)) if failed else RiskLevel.LOW
    )
    reasons = [check.reason for check in failed]
    return StaticRiskResult(checks=checks, risk_level=risk, reasons=reasons)
