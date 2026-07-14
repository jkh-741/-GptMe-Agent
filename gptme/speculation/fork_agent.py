from __future__ import annotations

from collections.abc import Callable, Iterable

from gptme.llm import reply
from gptme.llm.models import get_default_model, get_model
from gptme.logmanager import Log, prepare_messages
from gptme.message import Message
from gptme.policyguard.evaluator import evaluate_tool_use
from gptme.policyguard.types import PolicyAction
from gptme.tools import ToolUse, get_tools
from gptme.tools.base import get_path
from gptme.tools.read import execute_read
from gptme.tools.save import (
    check_for_placeholders,
    execute_append_impl,
    execute_save_impl,
)

from .overlay import OverlayWorkspace, use_overlay
from .types import (
    SpeculationContext,
    SpeculationRun,
    SpeculationStatus,
    SpeculativeToolEvent,
)

ToolExecutor = Callable[[ToolUse], Iterable[Message]]


def run_predicted_step_once(
    context: SpeculationContext,
    run: SpeculationRun,
    *,
    allow_writes: bool = False,
) -> SpeculationRun:
    """Generate one assistant response for the predicted prompt, then execute safely."""
    assistant_message = _generate_predicted_assistant(context, run)
    run.assistant_message = assistant_message.replace(quiet=True)
    return run_fork_once(
        context,
        run,
        assistant_message=assistant_message,
        allow_writes=allow_writes,
    )


def run_fork_once(
    context: SpeculationContext,
    run: SpeculationRun,
    assistant_message: Message | None = None,
    tool_executor: ToolExecutor | None = None,
    allow_writes: bool = False,
) -> SpeculationRun:
    """Run one deterministic speculative pass.

    This does not call a model yet.  It gives phase 2 a testable safety boundary:
    parse candidate tool calls, ask PolicyGuard for each call, execute only
    injected allowlisted work, and stop exactly on ask/deny.
    """
    message = assistant_message or Message("assistant", run.prediction.content)
    run.assistant_message = message.replace(quiet=True)
    runnable_tools = [
        tool_use
        for tool_use in ToolUse.iter_from_content(message.content)
        if tool_use.is_runnable
    ]

    for tool_use in runnable_tools:
        normalized, decision = evaluate_tool_use(
            tool_use,
            workspace=context.workspace,
        )
        run.policy_decisions.append(decision)

        if decision.action == PolicyAction.DENY:
            _record_tool_event(
                run,
                tool_use=tool_use,
                action="deny",
                risk=decision.risk_level.value,
                path=normalized.paths[0] if normalized.paths else None,
                reasons=decision.reasons,
            )
            run.finish(SpeculationStatus.BLOCKED)
            return run

        if decision.action == PolicyAction.ASK:
            _record_tool_event(
                run,
                tool_use=tool_use,
                action="ask",
                risk=decision.risk_level.value,
                path=normalized.paths[0] if normalized.paths else None,
                reasons=decision.reasons,
            )
            run.finish(SpeculationStatus.WAITING_CONFIRMATION)
            return run

        if tool_executor is not None:
            for result_message in tool_executor(tool_use):
                run.result_messages.append(result_message)
            _record_tool_event(
                run,
                tool_use=tool_use,
                action=_allowed_event_action(tool_use.tool),
                risk=decision.risk_level.value,
                path=normalized.paths[0] if normalized.paths else None,
                reasons=decision.reasons,
            )
            continue

        execution = _execute_allowed_tool(
            context,
            run,
            tool_use,
            allow_writes=allow_writes,
        )
        if execution == "paused":
            _record_tool_event(
                run,
                tool_use=tool_use,
                action="ask",
                risk=decision.risk_level.value,
                path=normalized.paths[0] if normalized.paths else None,
                reasons=["Speculative execution paused before tool side effects."],
            )
            run.finish(SpeculationStatus.WAITING_CONFIRMATION)
            return run
        if execution == "unsupported":
            _record_tool_event(
                run,
                tool_use=tool_use,
                action="ask",
                risk=decision.risk_level.value,
                path=normalized.paths[0] if normalized.paths else None,
                reasons=["Tool is not supported by first-version fork executor."],
            )
            run.finish(SpeculationStatus.WAITING_CONFIRMATION)
            return run

        _record_tool_event(
            run,
            tool_use=tool_use,
            action=_allowed_event_action(tool_use.tool),
            risk=decision.risk_level.value,
            path=normalized.paths[0] if normalized.paths else None,
            reasons=decision.reasons,
        )

    run.finish(SpeculationStatus.WAITING_CONFIRMATION)
    return run


def _generate_predicted_assistant(
    context: SpeculationContext,
    run: SpeculationRun,
) -> Message:
    model = context.model
    if model is None:
        default_model = get_default_model()
        if default_model is None:
            raise ValueError("No model loaded and no model specified")
        model = default_model.full

    predicted_log = Log(
        context.messages_snapshot
        + [Message("user", run.prediction.content, quiet=True)]
    )
    prepared_messages = prepare_messages(predicted_log.messages, context.workspace)
    tools = None
    if context.tool_format == "tool":
        tools = [tool for tool in get_tools() if tool.is_runnable]

    overlay = OverlayWorkspace(context.workspace, run.overlay_root)
    with use_overlay(overlay):
        return reply(
            prepared_messages,
            get_model(model).full,
            stream=False,
            tools=tools,
            workspace=context.workspace,
        )


def _execute_allowed_tool(
    context: SpeculationContext,
    run: SpeculationRun,
    tool_use: ToolUse,
    *,
    allow_writes: bool,
) -> str:
    overlay = OverlayWorkspace(context.workspace, run.overlay_root)
    with use_overlay(overlay):
        if tool_use.tool == "read":
            run.result_messages.extend(
                execute_read(tool_use.content, tool_use.args, tool_use.kwargs)
            )
            return "executed"

        if tool_use.tool in {"save", "append"}:
            if not allow_writes:
                return "paused"
            run.result_messages.extend(_execute_file_write_tool(tool_use))
            return "executed"

        if tool_use.tool == "shell":
            # PolicyGuard already rejected writes, deletes, network and other
            # medium+ shell risks before this point.
            run.result_messages.extend(tool_use.execute(workspace=context.workspace))
            return "executed"

    return "unsupported"


def _execute_file_write_tool(tool_use: ToolUse) -> list[Message]:
    content = tool_use.kwargs.get("content") if tool_use.kwargs else None
    if not content:
        content = tool_use.content
    if not content:
        return [Message("system", "No content provided")]
    if check_for_placeholders(content):
        return [
            Message(
                "system",
                "Speculative write aborted: content contains placeholder lines.",
            )
        ]

    path = get_path(content, tool_use.args, tool_use.kwargs)
    if not path:
        return [Message("system", "No path provided")]

    if tool_use.tool == "save":
        return list(execute_save_impl(content, path))
    if tool_use.tool == "append":
        return list(execute_append_impl(content, path))
    return [Message("system", f"Unsupported write tool: {tool_use.tool}")]


def _allowed_event_action(tool_name: str) -> str:
    return (
        "write" if tool_name in {"save", "append", "patch", "patch_many"} else "execute"
    )


def _record_tool_event(
    run: SpeculationRun,
    *,
    tool_use: ToolUse,
    action: str,
    risk: str | None,
    path,
    reasons: list[str],
) -> None:
    run.tool_events.append(
        SpeculativeToolEvent(
            tool_name=tool_use.tool,
            action=action,
            path=path,
            risk=risk,
            metadata={"reasons": reasons},
        )
    )
