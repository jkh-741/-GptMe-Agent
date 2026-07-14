from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import Any, Protocol

from gptme.llm import reply
from gptme.llm.models import get_default_model, get_model
from gptme.message import Message

from .types import PredictedPrompt, SpeculationConfig, SpeculationContext

logger = logging.getLogger(__name__)

PredictionLLM = Callable[[list[Message], str, Path | None], Message]


class Predictor(Protocol):
    def predict_next_prompts(
        self,
        context: SpeculationContext,
        config: SpeculationConfig,
    ) -> list[PredictedPrompt]: ...


class HeuristicPredictor:
    """Deterministic predictor used for early tests and manual experiments."""

    def predict_next_prompts(
        self,
        context: SpeculationContext,
        config: SpeculationConfig,
    ) -> list[PredictedPrompt]:
        last_user = next(
            (msg for msg in reversed(context.messages_snapshot) if msg.role == "user"),
            None,
        )
        if last_user is None:
            return []

        content = _predict_content(last_user.content)
        if not content:
            return []

        return [
            PredictedPrompt.create(
                content=content,
                confidence=0.8,
                reason="Heuristic continuation from the last user request.",
                ttl_seconds=config.ttl_seconds,
                allowed_tools_hint=["read", "rg"],
            )
        ][: context.max_predictions]


class ModelBackedPredictor:
    """Predict the next likely user prompt with a model, falling back safely."""

    def __init__(
        self,
        llm: PredictionLLM | None = None,
        fallback: Predictor | None = None,
    ) -> None:
        self.llm = llm or _default_prediction_llm
        self.fallback = fallback or HeuristicPredictor()

    def predict_next_prompts(
        self,
        context: SpeculationContext,
        config: SpeculationConfig,
    ) -> list[PredictedPrompt]:
        model = _prediction_model(context, config)
        if model is None:
            return self.fallback.predict_next_prompts(context, config)

        try:
            response = self.llm(_prediction_messages(context), model, context.workspace)
            predictions = _parse_prediction_response(
                response.content,
                config=config,
                max_predictions=context.max_predictions,
            )
        except Exception as exc:
            logger.debug("Model-backed speculation prediction failed: %s", exc)
            return self.fallback.predict_next_prompts(context, config)

        if not predictions:
            return self.fallback.predict_next_prompts(context, config)
        return predictions


def build_predictor(config: SpeculationConfig | None = None) -> Predictor:
    config = config or SpeculationConfig.from_env()
    if config.predictor_backend == "model":
        return ModelBackedPredictor()
    return HeuristicPredictor()


def _predict_content(last_user_content: str) -> str | None:
    text = last_user_content.strip()
    lowered = text.lower()
    if not text:
        return None
    if "test" in lowered or "测试" in text:
        return "继续补充并运行相关测试"
    if "plan" in lowered or "计划" in text:
        return "按照计划继续实现下一阶段"
    if "需求" in text or "requirements" in lowered:
        return "根据需求继续完善实现计划"
    return None


def _default_prediction_llm(
    messages: list[Message],
    model: str,
    workspace: Path | None,
) -> Message:
    return reply(
        messages,
        get_model(model).full,
        stream=False,
        workspace=workspace,
        temperature=0.2,
    )


def _prediction_model(
    context: SpeculationContext,
    config: SpeculationConfig,
) -> str | None:
    if config.predictor_model:
        return config.predictor_model
    if context.model:
        return context.model
    default_model = get_default_model()
    return default_model.full if default_model is not None else None


def _prediction_messages(context: SpeculationContext) -> list[Message]:
    recent = context.messages_snapshot[-8:]
    transcript = "\n".join(
        f"{message.role}: {message.content[:1200]}" for message in recent
    )
    available_tools = ", ".join(context.available_tools) or "unknown"
    prompt = dedent(
        f"""
        Predict the user's next likely message in this local coding-agent session.

        Return only JSON in this shape:
        {{
          "predictions": [
            {{
              "content": "likely next user message",
              "confidence": 0.0,
              "reason": "short reason",
              "allowed_tools_hint": ["read"]
            }}
          ]
        }}

        Constraints:
        - Predict the user's next message, not the assistant's next action.
        - Do not expand the user's authorization boundary.
        - Prefer concrete follow-up prompts that can be safely attempted.
        - Keep confidence between 0 and 1.
        - Available tools: {available_tools}

        Recent conversation:
        {transcript}
        """
    ).strip()
    return [
        Message(
            "system",
            "You predict the next user prompt for speculative execution.",
        ),
        Message("user", prompt),
    ]


def _parse_prediction_response(
    content: str,
    *,
    config: SpeculationConfig,
    max_predictions: int,
) -> list[PredictedPrompt]:
    payload = _load_json_object(content)
    raw_predictions = payload.get("predictions", payload)
    if isinstance(raw_predictions, dict):
        raw_predictions = [raw_predictions]
    if not isinstance(raw_predictions, list):
        return []

    predictions: list[PredictedPrompt] = []
    for item in raw_predictions[:max_predictions]:
        if not isinstance(item, dict):
            continue
        prediction = _prediction_from_mapping(item, config)
        if prediction is not None:
            predictions.append(prediction)
    return predictions


def _load_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        return {}
    return value


def _prediction_from_mapping(
    item: dict[str, Any],
    config: SpeculationConfig,
) -> PredictedPrompt | None:
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    confidence = _coerce_confidence(item.get("confidence"))
    reason = item.get("reason")
    allowed_tools = item.get("allowed_tools_hint")
    if not isinstance(reason, str):
        reason = "Model-backed speculation prediction."
    if not isinstance(allowed_tools, list):
        allowed_tools = []
    return PredictedPrompt.create(
        content=content.strip(),
        confidence=confidence,
        reason=reason.strip(),
        ttl_seconds=config.ttl_seconds,
        allowed_tools_hint=[tool for tool in allowed_tools if isinstance(tool, str)],
    )


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, int | float):
        return min(1.0, max(0.0, float(value)))
    if isinstance(value, str):
        try:
            return min(1.0, max(0.0, float(value)))
        except ValueError:
            return 0.0
    return 0.0
