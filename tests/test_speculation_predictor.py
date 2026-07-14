from __future__ import annotations

from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.speculation.manager import SpeculationManager
from gptme.speculation.predictor import (
    HeuristicPredictor,
    ModelBackedPredictor,
    build_predictor,
)
from gptme.speculation.types import (
    PredictedPrompt,
    SpeculationConfig,
    SpeculationContext,
)

if TYPE_CHECKING:
    from pathlib import Path


def _context(tmp_path: Path) -> SpeculationContext:
    workspace = tmp_path / "workspace"
    logdir = tmp_path / "log"
    workspace.mkdir()
    logdir.mkdir()
    messages = [
        Message("user", "帮我继续补测试"),
        Message("assistant", "我会先看测试结构。"),
    ]
    return SpeculationContext(
        conversation_id="test",
        logdir=logdir,
        workspace=workspace,
        messages_snapshot=messages,
        last_assistant_message=messages[-1],
        available_tools=["read", "shell"],
        model="mock/mock",
        max_predictions=2,
    )


def test_model_backed_predictor_parses_json_predictions(tmp_path: Path) -> None:
    def fake_llm(
        messages: list[Message],
        model: str,
        workspace: Path | None,
    ) -> Message:
        assert model == "mock/mock"
        assert workspace == tmp_path / "workspace"
        assert "Available tools: read, shell" in messages[-1].content
        return Message(
            "assistant",
            """
            {
              "predictions": [
                {
                  "content": "继续补测试并运行 pytest",
                  "confidence": 0.86,
                  "reason": "The user is iterating on tests.",
                  "allowed_tools_hint": ["read", "shell"]
                }
              ]
            }
            """,
        )

    predictions = ModelBackedPredictor(llm=fake_llm).predict_next_prompts(
        _context(tmp_path),
        SpeculationConfig(ttl_seconds=30),
    )

    assert len(predictions) == 1
    assert predictions[0].content == "继续补测试并运行 pytest"
    assert predictions[0].confidence == 0.86
    assert predictions[0].allowed_tools_hint == ["read", "shell"]


def test_model_backed_predictor_extracts_json_from_text(tmp_path: Path) -> None:
    def fake_llm(
        messages: list[Message],
        model: str,
        workspace: Path | None,
    ) -> Message:
        return Message(
            "assistant",
            'Here is JSON: {"content": "继续实现下一步", "confidence": "0.7"}',
        )

    predictions = ModelBackedPredictor(llm=fake_llm).predict_next_prompts(
        _context(tmp_path),
        SpeculationConfig(),
    )

    assert len(predictions) == 1
    assert predictions[0].content == "继续实现下一步"
    assert predictions[0].confidence == 0.7


def test_model_backed_predictor_falls_back_on_invalid_response(tmp_path: Path) -> None:
    def fake_llm(
        messages: list[Message],
        model: str,
        workspace: Path | None,
    ) -> Message:
        return Message("assistant", "not json")

    fallback_prediction = PredictedPrompt.create(
        "fallback prediction",
        0.8,
        "fallback",
        ttl_seconds=120,
    )

    class StaticFallback:
        def predict_next_prompts(
            self,
            context: SpeculationContext,
            config: SpeculationConfig,
        ) -> list[PredictedPrompt]:
            return [fallback_prediction]

    predictions = ModelBackedPredictor(
        llm=fake_llm,
        fallback=StaticFallback(),
    ).predict_next_prompts(_context(tmp_path), SpeculationConfig())

    assert predictions == [fallback_prediction]


def test_build_predictor_uses_model_backend_from_config() -> None:
    predictor = build_predictor(SpeculationConfig(predictor_backend="model"))

    assert isinstance(predictor, ModelBackedPredictor)


def test_manager_default_predictor_uses_config_backend() -> None:
    manager = SpeculationManager(
        config=SpeculationConfig(predictor_backend="heuristic")
    )

    assert isinstance(manager.predictor, HeuristicPredictor)
