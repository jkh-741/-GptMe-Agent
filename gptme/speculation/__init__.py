"""Speculative execution primitives for gptme.

The first version is intentionally inert unless explicitly enabled through
configuration.  It exposes internal building blocks that later phases can wire
into the chat loop.
"""

from .manager import SpeculationManager
from .predictor import HeuristicPredictor, ModelBackedPredictor, Predictor
from .types import (
    CommitResult,
    DiscardResult,
    PredictedPrompt,
    ResolutionAction,
    SpeculationConfig,
    SpeculationContext,
    SpeculationMetrics,
    SpeculationMode,
    SpeculationResolution,
    SpeculationRun,
    SpeculationStatus,
    SpeculativeToolEvent,
)

__all__ = [
    "CommitResult",
    "DiscardResult",
    "HeuristicPredictor",
    "ModelBackedPredictor",
    "PredictedPrompt",
    "Predictor",
    "ResolutionAction",
    "SpeculationConfig",
    "SpeculationContext",
    "SpeculationManager",
    "SpeculationMetrics",
    "SpeculationMode",
    "SpeculationResolution",
    "SpeculationRun",
    "SpeculationStatus",
    "SpeculativeToolEvent",
]
