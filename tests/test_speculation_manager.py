from __future__ import annotations

import os
from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.speculation.manager import SpeculationManager
from gptme.speculation.types import (
    PredictedPrompt,
    SpeculationConfig,
    SpeculationContext,
    SpeculationMode,
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


def _context(tmp_path: Path, completed_turns: int) -> SpeculationContext:
    workspace = tmp_path / "workspace"
    logdir = tmp_path / "log"
    workspace.mkdir()
    logdir.mkdir()
    messages = [
        Message("user", "帮我补测试"),
        Message("assistant", "可以，我会先看相关测试。"),
    ]
    return SpeculationContext(
        conversation_id="test",
        logdir=logdir,
        workspace=workspace,
        messages_snapshot=messages,
        last_assistant_message=messages[-1],
        completed_turns=completed_turns,
    )


def test_off_mode_does_not_call_predictor(tmp_path: Path) -> None:
    predictor = StaticPredictor(
        PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    )
    manager = SpeculationManager(
        config=SpeculationConfig(mode=SpeculationMode.OFF),
        predictor=predictor,
    )

    run = manager.start_speculation(_context(tmp_path, completed_turns=10))

    assert run is None
    assert predictor.calls == 0
    assert manager.active_runs == []


def test_auto_mode_waits_for_warmup_turns(tmp_path: Path) -> None:
    predictor = StaticPredictor(
        PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    )
    manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.AUTO,
            warmup_turns=3,
            overlay_root=tmp_path / "speculation",
        ),
        predictor=predictor,
    )

    run = manager.start_speculation(_context(tmp_path, completed_turns=2))

    assert run is None
    assert predictor.calls == 0


def test_auto_mode_starts_after_warmup(tmp_path: Path) -> None:
    prediction = PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.AUTO,
            warmup_turns=3,
            overlay_root=tmp_path / "speculation",
        ),
        predictor=StaticPredictor(prediction),
    )

    context = _context(tmp_path, completed_turns=3)
    run = manager.start_speculation(context)

    assert run is not None
    assert run in manager.active_runs
    assert run.overlay_root == (
        tmp_path / "speculation" / str(os.getpid()) / prediction.prediction_id
    )
    assert run.overlay_root.exists()
    assert (context.logdir / "speculation-events.jsonl").exists()


def test_low_confidence_prediction_is_skipped(tmp_path: Path) -> None:
    prediction = PredictedPrompt.create("继续补测试", 0.2, "test", ttl_seconds=120)
    manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.AUTO,
            warmup_turns=0,
            min_confidence=0.75,
            overlay_root=tmp_path / "speculation",
        ),
        predictor=StaticPredictor(prediction),
    )

    run = manager.start_speculation(_context(tmp_path, completed_turns=0))

    assert run is None
    assert manager.active_runs == []


def test_start_and_run_once_executes_single_speculative_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_tools(allowlist=["read"])
    monkeypatch.setenv("GPTME_POLICYGUARD_SEMANTIC_MODE", "off")
    prediction = PredictedPrompt.create("继续补测试", 0.9, "test", ttl_seconds=120)
    manager = SpeculationManager(
        config=SpeculationConfig(
            mode=SpeculationMode.MANUAL,
            overlay_root=tmp_path / "speculation",
        ),
        predictor=StaticPredictor(prediction),
    )
    context = _context(tmp_path, completed_turns=0)
    (context.workspace / "notes.txt").write_text("hello\n", encoding="utf-8")

    run = manager.start_and_run_once(
        context,
        assistant_message=Message("assistant", "```read notes.txt\n```"),
    )

    assert run is not None
    assert run.status == SpeculationStatus.WAITING_CONFIRMATION
    assert "hello" in run.result_messages[0].content
    assert (context.logdir / "speculation-events.jsonl").read_text(
        encoding="utf-8"
    ).count("speculation_") == 2
