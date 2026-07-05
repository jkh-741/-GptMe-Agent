# Task 1: Prompt Queue 持久性修复

## 1. 背景

当前 gptme 支持通过另一个终端向正在运行的会话发送后续 prompt。

典型调用链：

```text
gptme-util chats send <conversation_id> <message>
  -> gptme/cli/cmd_chats.py::chats_send()
  -> gptme/prompt_queue.py::queue_prompt()
  -> <logdir>/prompt-queue.jsonl

chat loop
  -> gptme/chat.py::_drain_external_prompt_queue()
  -> gptme/prompt_queue.py::drain_prompt_queue()
  -> prompt_queue.extend(drained)
  -> prompt_queue.pop(0)
  -> LogManager.append(msg)
  -> conversation.jsonl
```

`prompt` 是提示词。`queue` 是队列。这里的 Prompt Queue 指“等待当前会话后续处理的用户消息队列”。

## 2. 当前问题

`drain_prompt_queue()` 当前会先从 `prompt-queue.jsonl` 里取出消息，然后立刻重写或删除这个队列文件。

如果消息已经从 `prompt-queue.jsonl` 删除，但还没有成功写入 `conversation.jsonl`，进程此时崩溃或被强制终止，就会导致该 prompt 丢失。

核心风险窗口：

```text
prompt-queue.jsonl 已删除该消息
  -> 消息只存在于进程内存
  -> LogManager.append(msg) 尚未完成
  -> 进程崩溃
  -> 恢复会话后无法找回该 prompt
```

因此当前队列更接近“最多处理一次”，不是可靠的持久化队列。

## 3. 目标

把 Prompt Queue 改造成更可靠的持久化队列，保证外部发送的 prompt 不会因为 drain 后、append 前的崩溃窗口而丢失。

目标流程：

```text
queued
  -> claim
  -> append
  -> ack
```

`claim` 是认领，表示某条队列消息正在被当前 Agent 处理。

`ack` 是 acknowledgment 的缩写，中文是确认完成。这里表示消息已经安全写入会话日志，可以从队列文件中移除。

## 4. 功能需求

1. 队列记录需要有唯一标识，例如 `queue_id`。

2. `queue_prompt()` 写入新 prompt 时，应保存必要元数据：

```text
queue_id
content
queued_at
status
```

3. `drain_prompt_queue()` 不应在取出消息时立即删除记录，而应先将记录标记为 `inflight`。

4. 消息成功写入 `conversation.jsonl` 后，再执行 ack，把对应 `queue_id` 的队列记录从 `prompt-queue.jsonl` 中删除。

5. `Message` 或其 metadata 中需要能记录 `queue_id`，用于判断某条队列消息是否已经进入会话历史。

6. 会话恢复时，如果发现 `inflight` 记录但 `conversation.jsonl` 中没有对应 `queue_id`，应重新投递该 prompt。

7. 会话恢复时，如果发现 `conversation.jsonl` 中已经有对应 `queue_id`，则不应重复投递，只需要清理或 ack 队列记录。

8. `max_items` 仍然需要生效。超过本次容量的 prompt 应继续留在磁盘队列中，保持 FIFO 顺序。

`FIFO` 是 First In, First Out，中文是“先进先出”。

## 5. 非目标

本任务只解决 Prompt Queue 的持久性和崩溃恢复问题。

暂不实现：

1. 多模型任务调度。
2. 推测性执行。
3. PolicyGuard 安全审查。
4. 复杂优先级队列。
5. 分布式队列或跨机器同步。

## 6. 验收标准

1. 外部执行 `gptme-util chats send` 后，prompt 会以带 `queue_id` 的记录写入 `prompt-queue.jsonl`。

2. Agent drain 队列时，记录不会立刻丢失，而是进入可恢复的处理中状态。

3. `LogManager.append()` 成功后，对应队列记录才会被 ack 删除。

4. drain 后、append 前模拟崩溃，重启后该 prompt 仍能被重新处理。

5. append 后、ack 前模拟崩溃，重启后不会重复处理同一条 prompt。

6. 多条 prompt 按 FIFO 顺序处理。

7. `max_items` 限制下，未处理的 prompt 仍保留在磁盘文件中。

## 7. 建议测试

1. `queue_prompt()` 写入记录时包含 `queue_id`、`content`、`queued_at`、`status`。

2. `drain_prompt_queue(max_items=1)` 只 claim 一条，其余记录保留。

3. 模拟 claim 后未 append 的恢复路径，确认消息会重新投递。

4. 模拟 append 后未 ack 的恢复路径，确认消息不会重复投递。

5. 队列文件中存在 malformed JSON 行时，仍能跳过坏记录并处理正常记录。

6. 增加该功能后，不能影响正常的 gptme 对话功能。没有外部 queued prompt 时，普通启动、用户输入、模型回复、工具调用和会话日志写入流程应保持原有行为。

`malformed JSON` 指格式错误的 JSON 数据。

## 8. 简历表达方向

实现可靠 Prompt Queue 后，可以表述为：

```text
为本地代码 Agent 设计并实现基于 claim/ack 的持久化 prompt 队列，修复 drain 后、append 前崩溃导致消息丢失的问题，并通过 queue_id 保证恢复过程中的幂等处理。
```

`idempotent` 是幂等，意思是同一操作重复执行多次，最终效果仍然和执行一次一致。
