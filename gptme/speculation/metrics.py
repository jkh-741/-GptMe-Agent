from __future__ import annotations

from typing import TYPE_CHECKING

from .types import SpeculationMetrics, dumps_jsonl

if TYPE_CHECKING:
    from pathlib import Path


def write_metrics(logdir: Path, metrics: SpeculationMetrics) -> None:
    logdir.mkdir(parents=True, exist_ok=True)
    metrics_path = logdir / "speculation-metrics.jsonl"
    with metrics_path.open("a", encoding="utf-8") as f:
        f.write(dumps_jsonl(metrics) + "\n")
