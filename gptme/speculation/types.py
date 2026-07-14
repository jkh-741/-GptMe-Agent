from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from gptme.message import Message


class SpeculationMode(str, Enum):
    OFF = "off"
    MANUAL = "manual"
    AUTO = "auto"


class SpeculationStatus(str, Enum):
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    MATCHED = "matched"
    DISCARDED = "discarded"
    BLOCKED = "blocked"
    FAILED = "failed"


class ResolutionAction(str, Enum):
    COMMIT = "commit"
    REUSE_READONLY_RESULT = "reuse_readonly_result"
    DISCARD_ALL = "discard_all"
    ASK_USER = "ask_user"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SpeculationConfig:
    mode: SpeculationMode = SpeculationMode.OFF
    predictor_backend: str = "heuristic"
    predictor_model: str | None = None
    max_runs: int = 1
    max_depth: int = 1
    warmup_turns: int = 3
    ttl_seconds: int = 120
    overlay_root: Path = Path("/tmp/speculation")
    min_confidence: float = 0.75
    allow_writes: bool = False
    metrics_enabled: bool = False

    @classmethod
    def from_env(cls) -> SpeculationConfig:
        mode_raw = _env("GPTME_SPECULATION_MODE", SpeculationMode.OFF.value).lower()
        try:
            mode = SpeculationMode(mode_raw)
        except ValueError:
            mode = SpeculationMode.OFF

        return cls(
            mode=mode,
            predictor_backend=_env(
                "GPTME_SPECULATION_PREDICTOR",
                "heuristic",
            ).lower(),
            predictor_model=os.environ.get("GPTME_SPECULATION_PREDICTOR_MODEL"),
            max_runs=max(1, _env_int("GPTME_SPECULATION_MAX_RUNS", 1)),
            max_depth=max(1, _env_int("GPTME_SPECULATION_MAX_DEPTH", 1)),
            warmup_turns=max(0, _env_int("GPTME_SPECULATION_WARMUP_TURNS", 3)),
            ttl_seconds=max(1, _env_int("GPTME_SPECULATION_TTL_SECONDS", 120)),
            overlay_root=Path(
                _env("GPTME_SPECULATION_OVERLAY_ROOT", "/tmp/speculation")
            ),
            min_confidence=min(
                1.0,
                max(0.0, _env_float("GPTME_SPECULATION_MIN_CONFIDENCE", 0.75)),
            ),
            allow_writes=_env_bool("GPTME_SPECULATION_ALLOW_WRITES", False),
            metrics_enabled=_env_bool("GPTME_SPECULATION_METRICS", False),
        )


@dataclass(frozen=True)
class PredictedPrompt:
    prediction_id: str
    content: str
    confidence: float
    reason: str
    allowed_tools_hint: list[str] = field(default_factory=list)
    expires_at: datetime = field(
        default_factory=lambda: utc_now() + timedelta(seconds=120)
    )

    @classmethod
    def create(
        cls,
        content: str,
        confidence: float,
        reason: str,
        ttl_seconds: int,
        allowed_tools_hint: list[str] | None = None,
    ) -> PredictedPrompt:
        return cls(
            prediction_id=uuid4().hex,
            content=content,
            confidence=confidence,
            reason=reason,
            allowed_tools_hint=allowed_tools_hint or [],
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )

    @property
    def expired(self) -> bool:
        return utc_now() >= self.expires_at


@dataclass(frozen=True)
class SpeculationContext:
    conversation_id: str
    logdir: Path
    workspace: Path
    messages_snapshot: list[Message]
    last_assistant_message: Message
    available_tools: list[str] = field(default_factory=list)
    policy_mode: str = "default"
    tool_format: str = "markdown"
    model: str | None = None
    max_predictions: int = 1
    completed_turns: int = 0
    depth: int = 0


@dataclass(frozen=True)
class SpeculativeToolEvent:
    tool_name: str
    action: str
    path: Path | None = None
    risk: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpeculationRun:
    run_id: str
    prediction: PredictedPrompt
    status: SpeculationStatus
    overlay_root: Path
    fork_logdir: Path
    messages_snapshot_hash: str
    tool_events: list[SpeculativeToolEvent] = field(default_factory=list)
    policy_decisions: list[Any] = field(default_factory=list)
    assistant_message: Message | None = None
    result_messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    depth: int = 0

    @classmethod
    def create(
        cls,
        prediction: PredictedPrompt,
        overlay_root: Path,
        fork_logdir: Path,
        messages_snapshot_hash: str,
        depth: int = 0,
    ) -> SpeculationRun:
        return cls(
            run_id=uuid4().hex,
            prediction=prediction,
            status=SpeculationStatus.RUNNING,
            overlay_root=overlay_root,
            fork_logdir=fork_logdir,
            messages_snapshot_hash=messages_snapshot_hash,
            depth=depth,
        )

    def finish(self, status: SpeculationStatus) -> None:
        self.status = status
        self.finished_at = utc_now()


@dataclass(frozen=True)
class SpeculationResolution:
    user_msg: Message
    matched_run_id: str | None
    match_score: float
    action: ResolutionAction
    reason: str
    reused_messages: list[Message] = field(default_factory=list)


@dataclass(frozen=True)
class CommitResult:
    run_id: str
    committed_files: list[Path]
    skipped_files: list[Path]
    success: bool
    reason: str


@dataclass(frozen=True)
class DiscardResult:
    run_id: str
    removed_overlay: bool
    removed_fork_logdir: bool
    reason: str


@dataclass(frozen=True)
class SpeculationMetrics:
    speculation_mode: str
    warmup_turns: int
    completed_turns_before_speculation: int
    speculation_hit: bool = False
    speculation_reused: bool = False
    speculation_committed: bool = False
    speculation_discarded: bool = False
    time_saved_ms: int = 0
    overlay_bytes_written: int = 0
    tool_calls_preexecuted: int = 0
    created_at: datetime = field(default_factory=utc_now)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Message):
        return value.to_dict()
    if is_dataclass(value):
        return to_jsonable(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    return value


def dumps_jsonl(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
