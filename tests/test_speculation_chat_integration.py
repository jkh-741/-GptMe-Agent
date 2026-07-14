from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gptme.chat import _maybe_start_speculation, _resolve_active_speculation
from gptme.message import Message
from gptme.speculation.manager import SpeculationManager
from gptme.speculation.types import (
    PredictedPrompt,
    SpeculationConfig,
    SpeculationContext,
    SpeculationMode,
    SpeculationRun,
    SpeculationStatus,
)
from gptme.tools import init_tools

if TYPE_CHECKING:
    from pathlib import Path


class StaticPredictor:
    def __init__(self, prediction: PredictedPrompt | None) -> None:
        self.prediction = prediction
        self.calls = 0

    def predict_next_prompts(
        self,
        context: SpeculationContext,
        config: SpeculationConfig,
    ) -> list[PredictedPrompt]:
        self.calls += 1
        if self.prediction is None:
            return []
        return [self.prediction]


@dataclass
class FakeLog:
    messages: list[Message]


@dataclass
class FakeManager:
    name: str
    logdir: Path
    workspace: Path
    log: FakeLog

    def append(self, msg: Message) -> None:
        self.log.messages.append(msg)


def _manager(tmp_path: Path, messages: list[Message]) -> FakeManager:
    workspace = tmp_path / "workspace"
    logdir = tmp_path / "log"
    workspace.mkdir()
    logdir.mkdir()
    return FakeManager(
        name="test",
        logdir=logdir,
        workspace=workspace,
        log=FakeLog(messages),
    )


def test_chat_hook_off_mode_does_not_start_prediction(tmp_path: Path) -> None:
    predictor = StaticPredictor(
        PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    )
    speculation_manager = SpeculationManager(
        config=SpeculationConfig(mode=SpeculationMode.OFF),
        predictor=predictor,
    )
    manager = _manager(
        tmp_path,
        [Message("user", "补测试"), Message("assistant", "好的")],
    )

    _maybe_start_speculation(speculation_manager, manager)  # type: ignore[arg-type]

    assert predictor.calls == 0
    assert speculation_manager.active_runs == []


def test_chat_hook_auto_mode_starts_background_speculation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["read"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    prediction = PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    speculation_manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.AUTO,
            warmup_turns=1,
            overlay_root=tmp_path / "speculation",
        ),
        predictor=StaticPredictor(prediction),
    )
    manager = _manager(
        tmp_path,
        [
            Message("user", "帮我补测试"),
            Message("assistant", "好的"),
        ],
    )

    def fake_run_predicted_step_once(
        context: SpeculationContext,
        run: SpeculationRun,
        *,
        allow_writes: bool = False,
    ) -> SpeculationRun:
        run.assistant_message = Message("assistant", "我会先读取 notes.txt。")
        run.result_messages.append(Message("system", "hello"))
        run.finish(SpeculationStatus.WAITING_CONFIRMATION)
        return run

    monkeypatch.setattr(
        "gptme.speculation.manager.run_predicted_step_once",
        fake_run_predicted_step_once,
    )

    _maybe_start_speculation(speculation_manager, manager)  # type: ignore[arg-type]

    assert len(speculation_manager.active_runs) == 1
    run = speculation_manager.active_runs[0]
    speculation_manager._threads[run.run_id].join(timeout=2)
    assert run.result_messages
    assert "hello" in run.result_messages[0].content
    assert (manager.logdir / "speculation-events.jsonl").exists()


def test_chat_hook_reuses_readonly_speculation_messages(
    tmp_path: Path,
) -> None:
    speculation_manager = SpeculationManager(
        config=SpeculationConfig(mode=SpeculationMode.AUTO),
    )
    manager = _manager(
        tmp_path,
        [
            Message("user", "帮我补测试"),
            Message("assistant", "好的"),
            Message("user", "继续补测试"),
        ],
    )
    run = SpeculationRun.create(
        prediction=PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120),
        overlay_root=tmp_path / "overlay",
        fork_logdir=tmp_path / "log" / "speculation" / "run",
        messages_snapshot_hash="hash",
    )
    run.assistant_message = Message("assistant", "我已经提前读取测试文件。")
    run.result_messages.append(Message("system", "read result"))
    run.finish(SpeculationStatus.WAITING_CONFIRMATION)
    speculation_manager.active_runs.append(run)

    reused = _resolve_active_speculation(
        speculation_manager,
        manager,  # type: ignore[arg-type]
        Message("user", "继续补测试"),
    )

    assert reused is True
    assert speculation_manager.active_runs == []
    assert manager.log.messages[-2:] == [
        Message("assistant", "我已经提前读取测试文件。"),
        Message("system", "read result"),
    ]


def test_chat_hook_resolves_miss_by_discarding_overlay(
    tmp_path: Path,
) -> None:
    speculation_manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.AUTO,
            metrics_enabled=True,
        ),
    )
    manager = _manager(
        tmp_path,
        [
            Message("user", "帮我补测试"),
            Message("assistant", "好的"),
            Message("user", "写 Notion"),
        ],
    )
    run = SpeculationRun.create(
        prediction=PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120),
        overlay_root=tmp_path / "overlay",
        fork_logdir=tmp_path / "log" / "speculation" / "run",
        messages_snapshot_hash="hash",
    )
    run.overlay_root.mkdir()
    (run.overlay_root / "notes.txt").write_text("overlay\n", encoding="utf-8")
    speculation_manager.active_runs.append(run)

    _resolve_active_speculation(
        speculation_manager,
        manager,  # type: ignore[arg-type]
        Message("user", "写 Notion"),
    )

    assert speculation_manager.active_runs == []
    assert not run.overlay_root.exists()
    assert (manager.logdir / "speculation-events.jsonl").exists()
    assert (manager.logdir / "speculation-metrics.jsonl").exists()
