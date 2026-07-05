# Task 1 实现计划：Prompt Queue 持久性修复

## 目标

把当前 Prompt Queue 从“drain 时直接删除磁盘记录”改成更可靠的 `claim -> append -> ack` 流程，避免外部 queued prompt 在写入 `conversation.jsonl` 前因进程崩溃而丢失。

`claim` 是认领，表示队列消息正在被当前进程处理。`ack` 是 acknowledgment 的缩写，中文是确认完成，表示消息已经安全写入会话日志，可以从队列文件移除。

## 实现步骤

1. 梳理现有调用链

   重点阅读：

   ```text
   gptme/cli/cmd_chats.py::chats_send()
   gptme/prompt_queue.py::queue_prompt()
   gptme/prompt_queue.py::drain_prompt_queue()
   gptme/chat.py::_drain_external_prompt_queue()
   gptme/chat.py::_run_chat_loop()
   gptme/logmanager/manager.py::LogManager.append()
   gptme/message.py::Message
   ```

   需要确认当前 queued prompt 从磁盘进入内存，再进入 `conversation.jsonl` 的完整路径。

2. 扩展队列记录格式

   在 `queue_prompt()` 写入的 JSON Lines 记录中加入：

   ```text
   queue_id
   content
   queued_at
   status
   claimed_at
   ```

   `JSON Lines` 是一行一个 JSON 对象的文本格式。旧记录可能没有 `queue_id` 或 `status`，实现时需要兼容旧格式。

3. 调整 drain 行为为 claim

   将 `drain_prompt_queue()` 的语义从“取出并删除”改成“取出并标记为 inflight”。

   新流程：

   ```text
   读取 prompt-queue.jsonl
   -> 跳过 malformed JSON
   -> 选择 status 为 queued 的记录
   -> 写回 status=inflight、claimed_at
   -> 返回带 queue_id 的 Message
   ```

   `malformed JSON` 指格式错误的 JSON 数据。

4. 把 `queue_id` 带入 Message

   通过 `Message.metadata` 保存 `queue_id`，例如：

   ```text
   metadata.queue_id = <queue_id>
   ```

   这样后续可以判断某条 queued prompt 是否已经被写入 `conversation.jsonl`。

5. 在 append 成功后 ack

   在 `_run_chat_loop()` 中，queued prompt 被 `manager.append(msg)` 成功写入后，调用新的 ack 函数清理对应队列记录。

   目标调用链：

   ```text
   _drain_external_prompt_queue()
     -> drain_prompt_queue()
     -> prompt_queue.extend(drained)

   _run_chat_loop()
     -> msg = prompt_queue.pop(0)
     -> manager.append(msg)
     -> ack_prompt_queue_item(logdir, queue_id)
   ```

6. 增加恢复逻辑

   drain 时需要检查 `inflight` 记录：

   - 如果 `conversation.jsonl` 中已经存在相同 `queue_id`，说明 append 成功但 ack 失败，应执行 ack 清理；
   - 如果 `conversation.jsonl` 中不存在相同 `queue_id`，说明 claim 后 append 前崩溃，应重新投递；
   - 仍需遵守 `max_items`，未投递的记录保留在磁盘中。

7. 保持正常对话行为不变

   没有 `prompt-queue.jsonl` 时，普通 gptme 对话不应受到影响。

   需要重点确认：

   ```text
   普通交互输入
   命令行 prompt
   管道 prompt
   模型回复
   工具调用
   conversation.jsonl 写入
   ```

   这些流程在无外部 queued prompt 时保持原行为。

8. 补充测试

   优先补 `prompt_queue` 相关单元测试，再补一到两个 chat loop 集成路径测试。

   测试重点：

   ```text
   queue_prompt 写入新格式
   drain 时 claim 而不是删除
   claim 后未 append 的恢复
   append 后未 ack 的恢复
   max_items 保持 FIFO
   malformed JSON 跳过
   无队列时普通 chat 行为不变
   ```

   `FIFO` 是 First In, First Out，中文是“先进先出”。

## 预计改动文件

```text
gptme/prompt_queue.py
gptme/chat.py
gptme/message.py
tests/test_prompt_queue.py 或相邻测试文件
```

如果现有测试结构已有更合适文件，以项目当前测试组织为准。

## 风险点

1. 不能破坏旧格式队列文件，否则已有用户的 `prompt-queue.jsonl` 可能无法恢复。
2. 不能让同一个 `queue_id` 被重复 append，避免重复执行用户指令。
3. 不能因为队列锁或文件重写影响普通对话性能。
4. Windows 下没有 `fcntl`，现有锁在 Windows 上只是文件级占位，不是真正跨进程排他锁；本任务先保持现状，不扩大锁机制范围。

`fcntl` 是 Unix/Linux 系统上的文件控制接口，常用于文件锁。Windows 下通常不可用。

## 完成标准

1. 代码实现 `claim -> append -> ack`。
2. 旧队列格式兼容。
3. 崩溃恢复路径有测试覆盖。
4. 普通 gptme 对话路径不受影响。
5. 相关测试通过。
