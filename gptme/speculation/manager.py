from __future__ import annotations

import hashlib
import os
import threading
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .audit import write_audit_event
from .commit import discard_overlay
from .fork_agent import run_fork_once, run_predicted_step_once
from .matcher import resolve_speculation
from .metrics import write_metrics
from .overlay import OverlayWorkspace
from .predictor import Predictor, build_predictor
from .types import (
    PredictedPrompt,
    SpeculationConfig,
    SpeculationContext,
    SpeculationMetrics,
    SpeculationMode,
    SpeculationResolution,
    SpeculationRun,
    SpeculationStatus,
)

if TYPE_CHECKING:
    from gptme.message import Message


@dataclass
class SpeculationManager:
    config: SpeculationConfig = field(default_factory=SpeculationConfig.from_env)
    predictor: Predictor | None = None
    active_runs: list[SpeculationRun] = field(default_factory=list)
    _threads: dict[str, threading.Thread] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.predictor is None:
            self.predictor = build_predictor(self.config)

    def should_start(self, context: SpeculationContext) -> bool:
        if self.config.mode != SpeculationMode.AUTO:
            return False
        if context.completed_turns < self.config.warmup_turns:
            return False
        if context.depth >= self.config.max_depth:
            return False
        return len(self.active_runs) < self.config.max_runs

    def start_speculation(
        self,
        context: SpeculationContext,
    ) -> SpeculationRun | None:
        if self.config.mode == SpeculationMode.OFF:
            return None
        if self.config.mode == SpeculationMode.AUTO and not self.should_start(context):
            return None

        assert self.predictor is not None
        predictions = [
            prediction
            for prediction in self.predictor.predict_next_prompts(context, self.config)
            if _prediction_allowed(prediction, self.config)
        ]
        if not predictions:
            return None

        prediction = predictions[0]
        run = _create_run(context, prediction, self.config)
        OverlayWorkspace(context.workspace, run.overlay_root)
        self.active_runs.append(run)
        write_audit_event(
            context.logdir,
            {
                "event": "speculation_started",
                "run": run,
                "completed_turns": context.completed_turns,
                "warmup_turns": self.config.warmup_turns,
            },
        )
        if self.config.metrics_enabled:
            write_metrics(
                context.logdir,
                SpeculationMetrics(
                    speculation_mode=self.config.mode.value,
                    warmup_turns=self.config.warmup_turns,
                    completed_turns_before_speculation=context.completed_turns,
                ),
            )
        return run

    def start_and_run_once(
        self,
        context: SpeculationContext,
        assistant_message: Message | None = None,
    ) -> SpeculationRun | None:
        run = self.start_speculation(context)
        if run is None:
            return None

        run_fork_once(
            context,
            run,
            assistant_message=assistant_message,
            allow_writes=self.config.allow_writes,
        )
        write_audit_event(
            context.logdir,
            {
                "event": "speculation_finished",
                "run_id": run.run_id,
                "status": run.status.value,
                "tool_events": run.tool_events,
                "result_message_count": len(run.result_messages),
            },
        )
        return run

    def start_background(
        self,
        context: SpeculationContext,
        assistant_message: Message | None = None,
    ) -> SpeculationRun | None:
        run = self.start_speculation(context)
        if run is None:
            return None

        thread_context = copy_context()
        thread = threading.Thread(
            target=thread_context.run,
            args=(self._run_existing, context, run, assistant_message),
            name=f"gptme-speculation-{run.run_id[:8]}",
            daemon=True,
        )
        self._threads[run.run_id] = thread
        thread.start()
        return run

    def resolve_user_message(
        self,
        user_msg: Message,
        context: SpeculationContext,
    ) -> SpeculationResolution | None:
        if not self.active_runs:
            return None

        self._reap_finished_threads()
        resolution = resolve_speculation(user_msg, self.active_runs)
        matched_run = self._find_run(resolution.matched_run_id)

        if matched_run is None:
            discarded = [discard_overlay(run) for run in self.active_runs]
            write_audit_event(
                context.logdir,
                {
                    "event": "speculation_resolved",
                    "resolution": resolution,
                    "discarded": discarded,
                },
            )
            self.active_runs.clear()
            self._write_resolution_metrics(context, resolution, None)
            return resolution

        for run in list(self.active_runs):
            if run.run_id != matched_run.run_id:
                discard_overlay(run)
                self.active_runs.remove(run)

        if matched_run.status == SpeculationStatus.RUNNING:
            resolution = SpeculationResolution(
                user_msg=user_msg,
                matched_run_id=matched_run.run_id,
                match_score=resolution.match_score,
                action=resolution.action,
                reason="Predicted prompt matched, but speculative execution is still running.",
            )

        matched_run.finish(SpeculationStatus.MATCHED)
        discard_result = None
        if any(event.action == "write" for event in matched_run.tool_events):
            # Phase 3 intentionally refuses automatic commit.  A write hit must
            # be surfaced for explicit user authorization in a later UI layer.
            discard_result = discard_overlay(matched_run)
        else:
            reused_messages = [
                message
                for message in [
                    matched_run.assistant_message,
                    *matched_run.result_messages,
                ]
                if message is not None
            ]
            resolution = SpeculationResolution(
                user_msg=user_msg,
                matched_run_id=matched_run.run_id,
                match_score=resolution.match_score,
                action=resolution.action,
                reason=resolution.reason,
                reused_messages=reused_messages,
            )
        self.active_runs.remove(matched_run)
        self._threads.pop(matched_run.run_id, None)

        write_audit_event(
            context.logdir,
            {
                "event": "speculation_resolved",
                "resolution": resolution,
                "matched_run": matched_run,
                "discard_result": discard_result,
            },
        )
        self._write_resolution_metrics(context, resolution, matched_run)
        return resolution

    def block_run(self, run: SpeculationRun, reason: str) -> None:
        run.finish(SpeculationStatus.BLOCKED)
        write_audit_event(
            run.fork_logdir.parent,
            {"event": "speculation_blocked", "run_id": run.run_id, "reason": reason},
        )

    def _run_existing(
        self,
        context: SpeculationContext,
        run: SpeculationRun,
        assistant_message: Message | None,
    ) -> None:
        try:
            if assistant_message is None:
                run_predicted_step_once(
                    context,
                    run,
                    allow_writes=self.config.allow_writes,
                )
            else:
                run_fork_once(
                    context,
                    run,
                    assistant_message=assistant_message,
                    allow_writes=self.config.allow_writes,
                )
            write_audit_event(
                context.logdir,
                {
                    "event": "speculation_finished",
                    "run_id": run.run_id,
                    "status": run.status.value,
                    "tool_events": run.tool_events,
                    "result_message_count": len(run.result_messages),
                },
            )
        except Exception as exc:
            run.finish(SpeculationStatus.FAILED)
            write_audit_event(
                context.logdir,
                {
                    "event": "speculation_failed",
                    "run_id": run.run_id,
                    "error": str(exc),
                },
            )

    def _reap_finished_threads(self) -> None:
        for run_id, thread in list(self._threads.items()):
            if not thread.is_alive():
                thread.join(timeout=0)
                self._threads.pop(run_id, None)

    def _find_run(self, run_id: str | None) -> SpeculationRun | None:
        if run_id is None:
            return None
        return next((run for run in self.active_runs if run.run_id == run_id), None)

    def _write_resolution_metrics(
        self,
        context: SpeculationContext,
        resolution: SpeculationResolution,
        run: SpeculationRun | None,
    ) -> None:
        if not self.config.metrics_enabled:
            return
        write_metrics(
            context.logdir,
            SpeculationMetrics(
                speculation_mode=self.config.mode.value,
                warmup_turns=self.config.warmup_turns,
                completed_turns_before_speculation=context.completed_turns,
                speculation_hit=resolution.matched_run_id is not None,
                speculation_reused=run is not None
                and not any(event.action == "write" for event in run.tool_events),
                speculation_discarded=resolution.matched_run_id is None,
                overlay_bytes_written=(
                    OverlayWorkspace(
                        context.workspace, run.overlay_root
                    ).overlay_bytes_written()
                    if run is not None
                    else 0
                ),
                tool_calls_preexecuted=len(run.tool_events) if run is not None else 0,
            ),
        )


def _prediction_allowed(
    prediction: PredictedPrompt,
    config: SpeculationConfig,
) -> bool:
    return not prediction.expired and prediction.confidence >= config.min_confidence


def _create_run(
    context: SpeculationContext,
    prediction: PredictedPrompt,
    config: SpeculationConfig,
) -> SpeculationRun:
    snapshot_hash = _messages_hash(context.messages_snapshot)
    overlay_root = config.overlay_root / str(os.getpid()) / prediction.prediction_id
    fork_logdir = context.logdir / "speculation" / prediction.prediction_id
    return SpeculationRun.create(
        prediction=prediction,
        overlay_root=overlay_root,
        fork_logdir=fork_logdir,
        messages_snapshot_hash=snapshot_hash,
        depth=context.depth,
    )


def _messages_hash(messages: list[Message]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.role.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
