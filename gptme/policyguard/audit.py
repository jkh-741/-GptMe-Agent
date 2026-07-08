from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import NormalizedToolUse, PolicyDecision

logger = logging.getLogger(__name__)


def write_policy_event(
    *,
    logdir: Path | None,
    normalized: NormalizedToolUse,
    decision: PolicyDecision,
    confirmation_result: str | None = None,
) -> None:
    if logdir is None:
        return

    try:
        path = logdir / "policy-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": normalized.tool_name,
            "raw_content": normalized.raw_content,
            "normalized_args": normalized.to_semantic_args(),
            "semantic_mode": decision.semantic_mode,
            "semantic_result": _jsonable(decision.semantic_result),
            "fast_semantic_result": _jsonable(decision.fast_semantic_result),
            "thinking_semantic_result": _jsonable(
                decision.thinking_semantic_result
            ),
            "static_result": _jsonable(decision.static_result),
            "final_action": decision.action.value,
            "risk_level": decision.risk_level.value,
            "reasons": decision.reasons,
            "requires_explicit_confirmation": decision.requires_explicit_confirmation,
            "confirmation_result": confirmation_result,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_jsonable(event), ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write PolicyGuard audit event")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value
