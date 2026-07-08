from __future__ import annotations

import ast

from .types import PolicyCheckResult, RiskLevel, StaticRiskResult, max_risk

HIGH_RISK_CALLS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "os.system",
    "shutil.rmtree",
    "Path.unlink",
    "Path.rmdir",
    "Path.write_text",
    "Path.write_bytes",
    "eval",
    "exec",
    "__import__",
    "pickle.load",
    "importlib.import_module",
}

MEDIUM_RISK_MODULES = {"requests", "urllib", "socket", "dotenv"}
SENSITIVE_ATTRS = {"os.environ"}


def check_python_code(code: str) -> StaticRiskResult:
    checks: list[PolicyCheckResult] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        return StaticRiskResult(
            checks=[
                PolicyCheckResult(
                    name="python.ast_parse",
                    passed=False,
                    risk_level=RiskLevel.MEDIUM,
                    reason=f"Python code could not be parsed: {err.msg}.",
                    evidence={"line": err.lineno, "offset": err.offset},
                )
            ],
            risk_level=RiskLevel.MEDIUM,
            reasons=[f"Python code could not be parsed: {err.msg}."],
        )

    aliases = _collect_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = _resolve_name(node.func, aliases)
            if call_name in HIGH_RISK_CALLS:
                checks.append(
                    PolicyCheckResult(
                        name="python.dangerous_call",
                        passed=False,
                        risk_level=RiskLevel.HIGH,
                        reason=f"Python code calls high-risk API `{call_name}`.",
                        evidence={
                            "call": call_name,
                            "line": getattr(node, "lineno", None),
                        },
                    )
                )
            elif call_name == "open" and _open_writes(node):
                checks.append(
                    PolicyCheckResult(
                        name="python.file_write",
                        passed=False,
                        risk_level=RiskLevel.MEDIUM,
                        reason="Python code opens a file in write/append mode.",
                        evidence={"line": getattr(node, "lineno", None)},
                    )
                )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = _imported_modules(node)
            risky = sorted(imported & MEDIUM_RISK_MODULES)
            checks.extend(
                PolicyCheckResult(
                    name="python.risky_import",
                    passed=False,
                    risk_level=RiskLevel.MEDIUM,
                    reason=f"Python code imports network/environment-sensitive module `{module}`.",
                    evidence={"module": module, "line": getattr(node, "lineno", None)},
                )
                for module in risky
            )
        elif isinstance(node, ast.Attribute):
            attr_name = _resolve_name(node, aliases)
            if attr_name in SENSITIVE_ATTRS:
                checks.append(
                    PolicyCheckResult(
                        name="python.sensitive_attr",
                        passed=False,
                        risk_level=RiskLevel.HIGH,
                        reason=f"Python code accesses sensitive attribute `{attr_name}`.",
                        evidence={
                            "attribute": attr_name,
                            "line": getattr(node, "lineno", None),
                        },
                    )
                )

    if not checks:
        checks.append(
            PolicyCheckResult(
                name="python.ast",
                passed=True,
                risk_level=RiskLevel.LOW,
                reason="No high-risk Python AST patterns found.",
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


def _collect_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local_name = alias.asname or alias.name
                aliases[local_name] = f"{node.module}.{alias.name}"
    aliases["Path"] = aliases.get("Path", "Path")
    return aliases


def _resolve_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_name(node.value, aliases)
        if base.startswith("pathlib.Path"):
            base = "Path"
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _resolve_name(node.func, aliases)
    return ""


def _open_writes(node: ast.Call) -> bool:
    mode_arg: ast.AST | None = None
    if len(node.args) >= 2:
        mode_arg = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_arg = keyword.value
    return (
        isinstance(mode_arg, ast.Constant)
        and isinstance(mode_arg.value, str)
        and any(marker in mode_arg.value for marker in ("w", "a", "+", "x"))
    )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    return {node.module.split(".")[0]} if node.module else set()
