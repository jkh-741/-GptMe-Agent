import copy
import logging
import os
import threading
from collections.abc import Callable, Generator
from pathlib import Path

from .commands import execute_cmd
from .config import ChatConfig, get_config, require_workspace_exists
from .constants import (
    DECLINED_CONTENT,
    INTERRUPT_CONTENT,
    MAX_MESSAGE_LENGTH,
    MAX_PROMPT_QUEUE_SIZE,
)
from .constants import (
    prompt_user as prompt_user_styled,
)
from .hooks import HookType, trigger_hook
from .init import init
from .llm import reply
from .llm.models import get_default_model, get_model
from .logmanager import Log, LogManager, prepare_messages
from .message import Message, get_output_format, is_output_json, set_output_format
from .prompt_queue import (
    ack_prompt_queue_item,
    drain_prompt_queue,
    get_message_queue_id,
)
from .telemetry import set_conversation_context, trace_function
from .tools import (
    ToolFormat,
    ToolUse,
    execute_msg,
    get_tools,
)
from .tools.complete import SessionCompleteException
from .util import console, path_with_tilde
from .util.auto_naming import MAX_ASSISTANT_MSGS_FOR_NAMING, try_auto_name
from .util.context import include_paths
from .util.cost import log_costs
from .util.interrupt import clear_interruptible, set_interruptible
from .util.prompt import add_history, get_input
from .util.sound import print_bell
from .util.terminal import flush_stdin, set_current_conv_name, terminal_state_title

logger = logging.getLogger(__name__)


@trace_function(name="chat.main", attributes={"component": "chat"})
def chat(
    prompt_msgs: list[Message],
    initial_msgs: list[Message],
    logdir: Path,
    workspace: Path,
    model: str | None,
    stream: bool = True,
    no_confirm: bool = False,
    interactive: bool = True,
    show_hidden: bool = False,
    tool_allowlist: list[str] | None = None,
    tool_format: ToolFormat | None = None,
    output_schema: type | None = None,
    output_format: str = "text",
) -> None:
    """
    Run the chat loop.

    prompt_msgs: list of messages to execute in sequence.
    initial_msgs: list of history messages.
    workspace: path to workspace directory.

    Callable from other modules.

    中文说明：chat() 是对话运行入口，但它本身不直接调用模型完成全部工作。
    它先装配会话状态、日志、工作区、工具和输出格式，再把控制权交给
    _run_chat_loop()。真正的 Agent loop 是下面三层叠加出来的：
    _run_chat_loop() 负责不断接收用户消息；
    _process_message_conversation() 负责一轮消息内多次推进；
    step() 负责一次模型生成和工具执行。
    """
    # Set initial terminal title with conversation name
    conv_name = logdir.name
    set_current_conv_name(conv_name)

    # Set conversation context for telemetry
    # This propagates to all spans in this conversation
    set_conversation_context(conversation_id=conv_name)

    # tool_format should already be resolved by this point
    assert tool_format is not None, (
        "tool_format should be resolved before calling chat()"
    )

    # Apply output format (must happen before any rendering).
    # Save the caller's format so nested chat() calls (inline subagents) can
    # restore it on exit instead of unconditionally resetting to "text".
    _prev_output_format = get_output_format()
    try:
        set_output_format(output_format)

        # init
        # Mode detection for confirmation hooks is now handled inside init_hooks()
        # 中文说明：初始化模型、工具、命令和 hook。这里把 main() 准备好的配置
        # 真正应用到运行时，后续 step() 才能拿到默认模型和可用工具。
        init(model, interactive, tool_allowlist, tool_format, no_confirm)

        # Trigger session start hooks
        # 中文说明：会话开始 hook 可以追加系统消息，例如恢复待办、注入额外上下文。
        # hook 是扩展点，表示某个生命周期阶段触发的回调逻辑。
        if session_start_msgs := trigger_hook(
            HookType.SESSION_START,
            logdir=logdir,
            workspace=workspace,
            initial_msgs=initial_msgs,
        ):
            # Process any messages from session start hooks
            for hook_msg in session_start_msgs:
                initial_msgs = initial_msgs + [hook_msg]

        default_model = get_default_model()
        # Only require default_model if no explicit model was passed
        # Use nested if/else for proper mypy type narrowing
        if model is None:
            if default_model is None:
                raise ValueError("No model loaded and no model specified")
            model_to_use = default_model.full
        else:
            model_to_use = model
        modelmeta = get_model(model_to_use)
        if not modelmeta.supports_streaming and stream:
            logger.info(
                "Disabled streaming for '%s/%s' model (not supported)",
                modelmeta.provider,
                modelmeta.model,
            )
            stream = False

        if not is_output_json():
            console.log(f"Using logdir: {path_with_tilde(logdir)}")
        # 中文说明：LogManager 是会话状态管理器。它把 initial_msgs 作为初始历史，
        # 并负责后续所有 Message 的追加、打印和写入 conversation.jsonl。
        manager = LogManager.load(logdir, initial_msgs=initial_msgs, create=True)

        # Note: todo replay is now handled via SESSION_START hook

        # Initialize workspace
        if not is_output_json():
            console.log(f"Using workspace: {path_with_tilde(workspace)}")
        require_workspace_exists(workspace)
        # 中文说明：切换进 workspace 后，shell、patch、read 等工具默认都围绕
        # 这个项目目录工作，避免工具在错误目录里执行。
        os.chdir(workspace)

        # print log (suppressed in JSON output mode)
        if not is_output_json():
            manager.log.print(show_hidden=show_hidden)
            console.print("--- ^^^ past messages ^^^ ---")

        # Note: todo replay is now handled via SESSION_START hook
        # Note: Confirmation is now handled within ToolUse.execute() using the hook system,
        # so we no longer need to create and pass confirm_func.

        # Convert prompt_msgs to a queue for unified handling
        # 中文说明：把 main() 传进来的初始用户消息转成队列。这样命令行 prompt、
        # 管道 prompt、外部排队 prompt 和交互输入，都能走同一套循环处理。
        prompt_queue = list(prompt_msgs)

        # main loop
        # 中文说明：这里进入真正的外层 Agent loop。它会不断取下一条用户消息，
        # 调用模型，执行工具，然后决定继续下一轮还是回到用户输入。
        _run_chat_loop(
            manager,
            prompt_queue,
            stream,
            tool_format=tool_format,
            model=None,  # Pass None to allow dynamic model switching via /model command
            interactive=interactive,
            no_confirm=no_confirm,
            logdir=logdir,
            output_schema=output_schema,
        )
    except SessionCompleteException as e:
        if not is_output_json():
            console.log(f"Autonomous mode: {e}. Exiting.")

        # Trigger session end hooks
        if session_end_msgs := trigger_hook(
            HookType.SESSION_END, logdir=logdir, manager=manager
        ):
            for msg in session_end_msgs:
                manager.append(msg)
    finally:
        # Restore the caller's format so nested chat() calls (inline subagents)
        # don't clobber the parent's JSON mode when they exit.
        set_output_format(_prev_output_format)


def _run_chat_loop(
    manager,
    prompt_queue,
    stream,
    tool_format=None,
    model=None,
    interactive=True,
    no_confirm=False,
    logdir=None,
    output_schema=None,
):
    """Main chat loop - extracted to allow clean exception handling."""

    while True:
        # 中文说明：先把其他终端或后台写入的 durable prompt queue 合并进内存队列。
        # durable 表示这些排队消息已经持久化，不只存在当前进程内存里。
        _drain_external_prompt_queue(manager, prompt_queue)
        msg: Message | None = None
        try:
            # Process next message (either from prompt queue or user input)
            if prompt_queue:
                # 中文说明：优先处理队列中的消息。队列里的消息可能来自命令行参数、
                # 管道输入、多段 prompt，或者外部命令追加的 prompt。
                msg = prompt_queue.pop(0)
                assert msg is not None, "prompt_queue contained None"
                msg = include_paths(msg, manager.workspace)
                manager.append(msg)
                ack_prompt_queue_item(manager.logdir, get_message_queue_id(msg))

                # Handle user commands
                if msg.role == "user" and execute_cmd(msg, manager):
                    continue

                if msg.role == "user":
                    if turn_pre_msgs := trigger_hook(
                        HookType.TURN_PRE,
                        manager=manager,
                    ):
                        for hook_msg in turn_pre_msgs:
                            manager.append(hook_msg)

                # Process the message and get response
                try:
                    _process_message_conversation(
                        manager, stream, tool_format, model, output_schema
                    )
                except SessionCompleteException:
                    _drain_external_prompt_queue(manager, prompt_queue)
                    if not prompt_queue:
                        # No more prompts, properly exit
                        raise
                    # More chained prompts remain — continue processing them
                    logger.debug(
                        "complete called but %d chained prompts remain, continuing",
                        len(prompt_queue),
                    )
                    continue
            else:
                # Get user input or exit if non-interactive
                if not interactive:
                    logger.debug("Non-interactive and exhausted prompts")
                    break

                # 中文说明：队列空了才向用户要新输入。交互模式下，这就是终端里
                # 等待用户继续输入下一句话的位置。
                user_input = _get_user_input(manager.log, manager.workspace)
                if user_input is None:
                    # Either user wants to exit OR we should generate response directly
                    if _should_prompt_for_input(manager.log):
                        # User wants to exit
                        break
                    # Don't prompt for input, generate response directly (crash recovery, etc.)
                    # Process existing log without adding new message
                    _process_message_conversation(
                        manager,
                        stream,
                        tool_format,
                        model,
                        output_schema,
                    )
                else:
                    # Normal case: user provided input
                    msg = user_input
                    manager.append(msg)

                    # Reset interrupt flag since user provided new input

                    # Handle user commands
                    if msg.role == "user" and execute_cmd(msg, manager):
                        continue

                    # Trigger turn.pre hooks once per submitted user prompt,
                    # after the prompt is appended and command handling has
                    # declined to consume it.
                    if turn_pre_msgs := trigger_hook(
                        HookType.TURN_PRE,
                        manager=manager,
                    ):
                        for hook_msg in turn_pre_msgs:
                            manager.append(hook_msg)

                    # Process the message and get response
                    _process_message_conversation(
                        manager,
                        stream,
                        tool_format,
                        model,
                        output_schema,
                    )

            # Trigger LOOP_CONTINUE hooks to check if we should continue/exit
            # This handles auto-reply mechanism and other loop control logic
            # 中文说明：一轮结束后，hook 可以决定是否自动追加下一条 prompt。
            # 这让“自动继续”“后台队列”等能力不用塞进主循环硬编码。
            if loop_msgs := trigger_hook(
                HookType.LOOP_CONTINUE,
                manager=manager,
                interactive=interactive,
                prompt_queue=prompt_queue,
                no_confirm=no_confirm,
            ):
                for msg in loop_msgs:
                    # Add hook-generated messages to prompt queue with size limit
                    if len(prompt_queue) >= MAX_PROMPT_QUEUE_SIZE:
                        logger.warning(
                            f"Prompt queue limit ({MAX_PROMPT_QUEUE_SIZE}) reached, "
                            "dropping message from hook"
                        )
                        break
                    prompt_queue.append(msg)
                    if not is_output_json():
                        console.log(f"[Loop control] {msg.content[:100]}...")
                continue  # Process the queued messages

        except KeyboardInterrupt:
            if not is_output_json():
                console.log("Interrupted.")
            manager.append(Message("system", INTERRUPT_CONTENT))
            # Clear any remaining prompts to avoid confusion
            prompt_queue.clear()
            continue

    # Trigger session end hooks when exiting normally
    if session_end_msgs := trigger_hook(
        HookType.SESSION_END, logdir=logdir, manager=manager
    ):
        for msg in session_end_msgs:
            manager.append(msg)


def _drain_external_prompt_queue(
    manager: LogManager, prompt_queue: list[Message]
) -> None:
    """Merge any durable queued prompts into the in-memory queue."""
    capacity = MAX_PROMPT_QUEUE_SIZE - len(prompt_queue)
    if capacity <= 0:
        return

    queued_ids = {
        queue_id
        for msg in prompt_queue
        if (queue_id := get_message_queue_id(msg)) is not None
    }
    drained = drain_prompt_queue(
        manager.logdir,
        max_items=capacity,
        exclude_queue_ids=queued_ids,
    )
    if not drained:
        return

    prompt_queue.extend(drained)
    logger.info("Loaded %d queued prompt(s) for %s", len(drained), manager.logdir.name)


def _process_message_conversation(
    manager: LogManager,
    stream: bool,
    tool_format: ToolFormat,
    model: str | None,
    output_schema: type | None = None,
) -> None:
    """Process a message and generate responses until no more tools to run.

    Note: Confirmation is now handled within ToolUse.execute() using the hook system.

    中文说明：这是内层 Agent loop。一次用户输入可能不止触发一次模型调用：
    模型先回复并写出工具调用，工具执行后把结果写回日志，随后模型继续读取
    新日志再生成下一步。循环直到最后一条助手消息里没有可执行工具为止。
    """
    max_steps: int | None = None
    max_steps_str = os.environ.get("GPTME_MAX_STEPS")
    if max_steps_str:
        try:
            max_steps = int(max_steps_str)
        except ValueError:
            logger.warning(
                f"Invalid GPTME_MAX_STEPS value: {max_steps_str!r}, ignoring"
            )
    step_count = 0

    while True:
        try:
            set_interruptible()

            # Trigger pre-process hooks (step.pre - before each step in a turn)
            # 中文说明：每个 step 前都可以通过 hook 注入额外消息或检查状态。
            # step 指“一次模型生成 + 可能的工具执行”。
            if pre_msgs := trigger_hook(
                HookType.STEP_PRE,
                manager=manager,
            ):
                for msg in pre_msgs:
                    manager.append(msg)

            # 中文说明：step() 是 generator，因为一次 step 的产物不是固定的一条
            # 模型回复，而是“1 条 assistant 回复 + 0 到多条工具结果”。一条
            # assistant 消息可以调用多个工具，每个工具及其执行前后 hook 又都
            # 可能产生多条 Message，所以 step() 用 yield/yield from 统一展开。
            # 理论上也可以改成 return list[Message]，但不能直接 return 单条
            # Message，否则会丢失工具结果。这里立即 list() 会完整消费 generator，
            # 因此当前实现会等整个 step 执行完，再统一把消息写入 LogManager。
            response_msgs = list(
                step(
                    manager.log,
                    stream,
                    tool_format=tool_format,
                    workspace=manager.workspace,
                    model=model,
                    output_schema=output_schema,
                )
            )
        except KeyboardInterrupt:
            if not is_output_json():
                console.log("Interrupted during response generation.")
            manager.append(Message("system", INTERRUPT_CONTENT))
            break
        finally:
            clear_interruptible()

        for response_msg in response_msgs:
            # 中文说明：step() 产出的助手消息和工具结果都要写回 LogManager。
            # 这样下一次模型调用才能看到刚刚的工具执行结果。
            manager.append(response_msg)
            # run any user-commands, if msg is from user
            # 中文说明：reply() 本身固定返回 assistant Message；这里出现 user
            # 角色，只可能是 execute_msg() 执行工具时，由自定义工具、插件工具
            # 或 TOOL_EXECUTE_PRE/POST hook 额外产出的 Message，因为工具输出没有
            # 被限制为 system 角色。例如插件工具返回
            # Message("user", "/model deepseek/deepseek-chat")，step() 会通过
            # yield from execute_msg() 将它向上传递，此处再按用户命令执行 /model。
            # 当前内置工具的正常结果基本都是 system，这个分支主要保留扩展能力。
            if response_msg.role == "user" and execute_cmd(response_msg, manager):
                return

        # Check if user declined execution - return to prompt without generating response
        # This makes "n" at confirm prompt behave like Ctrl+C (return to user prompt)
        if any(msg.content == DECLINED_CONTENT for msg in response_msgs):
            if not is_output_json():
                console.log("Execution declined, returning to prompt.")
            break

        # Auto-generate display name in background thread to avoid blocking.
        # Shared logic with server in gptme/util/auto_naming.py::try_auto_name.
        # Pre-check assistant count to avoid spawning threads + doing disk I/O
        # after the naming window has closed (> MAX_ASSISTANT_MSGS_FOR_NAMING).
        current_model = get_default_model()
        assistant_count = sum(1 for m in manager.log.messages if m.role == "assistant")
        if current_model and 1 <= assistant_count <= MAX_ASSISTANT_MSGS_FOR_NAMING:
            chat_config = ChatConfig.from_logdir(manager.logdir)
            if not chat_config.name:
                thread = threading.Thread(
                    target=try_auto_name,
                    args=(
                        chat_config,
                        copy.deepcopy(manager.log.messages),
                        current_model.full,
                    ),
                    daemon=True,
                )
                thread.start()

        # Check step limit (GPTME_MAX_STEPS)
        step_count += 1
        if max_steps is not None and step_count >= max_steps:
            if not is_output_json():
                console.log(f"Reached max steps limit ({max_steps}), stopping.")
            manager.append(
                Message("system", f"Stopped: reached max steps limit ({max_steps})")
            )
            break

        # Check if there are any runnable tools left
        last_content = next(
            (m.content for m in reversed(manager.log) if m.role == "assistant"),
            "",
        )
        # 中文说明：如果最后一条助手消息里还有可执行工具调用，就继续下一次 step。
        # 如果没有工具调用，说明这一轮用户请求已经自然结束，回到外层循环。
        has_runnable = any(
            tooluse.is_runnable for tooluse in ToolUse.iter_from_content(last_content)
        )
        if not has_runnable:
            break

    # Trigger post-process hooks after message processing completes (turn.post)
    # Note: pre-commit checks and autocommit are now handled by hooks
    if post_msgs := trigger_hook(
        HookType.TURN_POST,
        manager=manager,
    ):
        for msg in post_msgs:
            manager.append(msg)


def _should_prompt_for_input(log: Log) -> bool:
    """
    Determine if we should ask for user input or generate response directly.

    Returns True if we should prompt for input, False if we should generate response.
    This preserves the original logic for handling edge cases like crash recovery.
    """
    last_msg = log[-1] if log else None

    # Check if there's an interrupt or decline message after the last assistant message
    # This handles cases where hooks (like cost_awareness) add messages after the interrupt/decline
    has_recent_interrupt_or_decline = False
    for msg in reversed(log):
        if msg.role == "assistant":
            break
        if msg.content in (INTERRUPT_CONTENT, DECLINED_CONTENT):
            has_recent_interrupt_or_decline = True
            break

    # Ask for input when:
    # - No messages at all
    # - Last message was from assistant (normal flow)
    # - There was an interrupt or decline after the last assistant message
    # - Last message was pinned
    # - No user messages exist in the entire log
    return (
        not last_msg
        or last_msg.role == "assistant"
        or has_recent_interrupt_or_decline
        or last_msg.pinned
        or not any(role == "user" for role in [m.role for m in log])
    )


def _get_user_input(log: Log, workspace: Path | None) -> Message | None:
    """Get user input, returning None if user wants to exit."""
    clear_interruptible()  # Don't interrupt during user input

    # Check if we should prompt for input or generate response directly
    if not _should_prompt_for_input(log):
        # Last message was from user (crash recovery, edited log, etc.)
        # Don't ask for input, let the system generate a response
        return None

    # print diff between now and last user message timestamp
    if get_config().get_env_bool("GPTME_SHOW_WORKED"):
        last_user_msg = next((m for m in reversed(log) if m.role == "user"), None)
        if last_user_msg and log:
            diff = log[-1].timestamp - last_user_msg.timestamp
            console.log(f"Worked for {diff.total_seconds():.2f} seconds")

    try:
        inquiry = prompt_user()
        # Validate message length to prevent unbounded memory usage
        truncation_suffix = "\n\n[Message truncated due to length]"
        if len(inquiry) > MAX_MESSAGE_LENGTH:
            logger.warning(
                f"Message truncated from {len(inquiry)} to {MAX_MESSAGE_LENGTH} chars"
            )
            # Account for suffix length to stay within MAX_MESSAGE_LENGTH
            inquiry = (
                inquiry[: MAX_MESSAGE_LENGTH - len(truncation_suffix)]
                + truncation_suffix
            )
        msg = Message("user", inquiry, quiet=True)
        msg = include_paths(msg, workspace)
        return msg
    except (EOFError, KeyboardInterrupt):
        return None


@trace_function(name="chat.step", attributes={"component": "chat"})
def step(
    log: Log | list[Message],
    stream: bool,
    tool_format: ToolFormat = "markdown",
    workspace: Path | None = None,
    model: str | None = None,
    output_schema: type | None = None,
    on_token: Callable[[str], None] | None = None,
) -> Generator[Message, None, None]:
    """Runs a single pass of the chat - generates response and executes tools.

    中文说明：step() 是 Agent loop 的最小执行单元。它先整理上下文，
    再调用模型生成助手消息，最后解析并执行助手消息里的工具调用。它使用
    generator 是因为产物数量不固定：首先 yield 模型生成的 assistant Message，
    然后通过 ``yield from execute_msg()`` 继续产出每个工具及其 hook 返回的
    Message。调用方可以选择逐条消费，也可以像当前实现一样用 ``list()``
    一次性收集。
    """
    default_model = get_default_model()
    # Only require default_model if no explicit model was passed
    # Use nested if/else for proper mypy type narrowing
    if model is None:
        if default_model is None:
            raise ValueError("No model loaded and no model specified")
        model = default_model.full
    if isinstance(log, list):
        log = Log(log)

    # Generate response and run tools
    try:
        set_interruptible()

        # performs reduction/context trimming, if necessary
        # 中文说明：prepare_messages() 在消息发送给模型前依次完成以下处理：
        # 1. 将消息所附文本文件的内容嵌入消息，图片和二进制附件留给模型适配层处理；
        # 2. 会话超过预设token阈值时，压缩最长且未固定、非工具调用的消息；
        # 3. 删除生存轮数已耗尽的临时消息，并合并因此产生的连续同角色消息；
        # 4. 按模型上下文窗口保留开头的系统消息和最近消息，同时移除失去
        #    对应工具调用的孤立工具结果，最终返回可传给 reply() 的消息列表。
        msgs = prepare_messages(log.messages, workspace)

        tools = None
        if tool_format == "tool":
            # 中文说明：当使用原生工具调用格式时，把可执行工具描述传给模型 API。
            # API 是 Application Programming Interface，中文是“应用程序编程接口”。
            tools = [t for t in get_tools() if t.is_runnable]

        # generate response
        # 中文说明：reply() 是模型调用入口。它根据 model 选择具体 provider，
        # 例如 DeepSeek、OpenAI 或 Anthropic，然后返回一条 assistant Message。
        with terminal_state_title("🤔 generating"):
            msg_response = reply(
                msgs,
                get_model(model).full,
                stream,
                tools,
                workspace,
                output_schema,
                on_token=on_token,
            )
            if get_config().get_env_bool("GPTME_COSTS"):
                log_costs(msgs + [msg_response])

        # Trigger generation post hooks (e.g., TTS)
        if generation_post_msgs := trigger_hook(
            HookType.GENERATION_POST,
            message=msg_response,
            workspace=workspace,
        ):
            for msg in generation_post_msgs:
                logger.debug(f"Generation post hook yielded: {msg}")

        # log response and run tools
        if msg_response:
            # 第一条产物固定是模型生成的 assistant Message。
            yield msg_response.replace(quiet=True)
            # 中文说明：execute_msg() 会扫描assistant消息中的工具调用并执行。
            # 每个工具可以返回一条或多条 Message，工具执行前后的 hook 也可以
            # 产生 Message，因此这里继续使用 yield from 将所有结果逐条向上传递。
            # 这些后续消息通常是 system，但扩展工具和 hook 也可以返回 user。
            yield from execute_msg(msg_response, log=log, workspace=workspace)

    finally:
        clear_interruptible()


def prompt_user(value=None) -> str:  # pragma: no cover
    print_bell()
    flush_stdin()
    response = ""
    # Get user name from config for the prompt display
    user_name = get_config().user.user.name
    styled_prompt = prompt_user_styled(user_name)
    with terminal_state_title("⌨️ waiting for input"):
        while not response:
            try:
                set_interruptible()
                response = prompt_input(styled_prompt, value)
                if response:
                    add_history(response)
            except KeyboardInterrupt:
                print("\nInterrupted. Press Ctrl-D to exit.")
            except EOFError:
                raise  # Let _get_user_input handle the normal exit flow
    clear_interruptible()
    return response


def prompt_input(prompt: str, value=None) -> str:  # pragma: no cover
    """Get input using prompt_toolkit with fish-style suggestions."""
    prompt = prompt.strip() + ": "
    if value:
        console.print(prompt + value)
        return value

    return get_input(prompt)
