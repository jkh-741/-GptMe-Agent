from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .types import ResolutionAction, SpeculationResolution, SpeculationRun

if TYPE_CHECKING:
    from gptme.message import Message


def resolve_speculation(
    user_msg: Message,
    active_runs: list[SpeculationRun],
    threshold: float = 0.6,
) -> SpeculationResolution:
    best_run: SpeculationRun | None = None
    best_score = 0.0
    for run in active_runs:
        if run.prediction.expired:
            continue
        score = match_score(user_msg.content, run.prediction.content)
        if score > best_score:
            best_score = score
            best_run = run

    if best_run is None or best_score < threshold:
        return SpeculationResolution(
            user_msg=user_msg,
            matched_run_id=None,
            match_score=best_score,
            action=ResolutionAction.DISCARD_ALL,
            reason="No active speculation matched the user message.",
        )

    changed_files = [event for event in best_run.tool_events if event.action == "write"]
    action = (
        ResolutionAction.ASK_USER
        if changed_files
        else ResolutionAction.REUSE_READONLY_RESULT
    )
    return SpeculationResolution(
        user_msg=user_msg,
        matched_run_id=best_run.run_id,
        match_score=best_score,
        action=action,
        reason="Predicted prompt matched the user message.",
    )


def match_score(actual: str, predicted: str) -> float:
    actual_terms = _terms(actual)
    predicted_terms = _terms(predicted)
    if not actual_terms or not predicted_terms:
        return 0.0
    intersection = actual_terms & predicted_terms
    union = actual_terms | predicted_terms
    return len(intersection) / len(union)


def _terms(text: str) -> set[str]:
    terms = {
        term
        for term in re.split(r"[^\w\u4e00-\u9fff]+", text.lower())
        if len(term) >= 2
    }
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    terms.update(
        "".join(cjk_chars[index : index + 2])
        for index in range(max(0, len(cjk_chars) - 1))
    )
    return terms
