# Task 3: Speculative Execution 推测性执行加速

## 1. 背景

前两阶段已经分别解决了两个基础问题：

```text
Task 1: Prompt Queue 持久性修复
  -> 让外部发送到会话的 prompt 不会因为崩溃窗口丢失。

Task 2: PolicyGuard 双重安全筛查
  -> 让高风险工具在真正执行前先经过语义审查和静态规则检查。
```

第三阶段的重点不是“提前在隔离环境里评测多个候选代码方案”，而是利用用户阅读、思考、输入下一句话的空档，让 Agent 提前预测用户可能的下一步请求，并在安全边界内先执行一部分工作，从而降低用户下一轮等待时间，提高整体吞吐率。

这里的 `Speculative Execution` 指“推测性执行”。可以把它理解成 CPU 分支预测的 AI Agent 版本：

```text
CPU:
  预测下一条分支会走哪里
  -> 先执行预测路径上的指令
  -> 猜对就提交结果
  -> 猜错就丢弃结果

Agent:
  预测用户下一步可能输入什么
  -> fork 一个推测 agent 先执行
  -> 写操作进入 Copy-on-Write overlay
  -> 用户确认匹配就提交 overlay
  -> 不匹配就删除 overlay
```

`fork agent` 指从当前会话状态复制出一个后台 Agent 分支。`overlay` 是覆盖层目录。`Copy-on-Write` 简称 `COW`，中文是“写时复制”：读文件时优先读 overlay 中已经修改过的版本；写文件时不直接改主工作区，而是把修改写到 overlay 临时目录。

## 2. 类比

餐厅服务员在你看菜单时，猜测你可能会点招牌菜，于是提前让厨房开始做：

```text
猜你会点招牌菜
  -> 厨房先做
  -> 你真的点了招牌菜
  -> 直接上菜，省等待时间

猜你会点招牌菜
  -> 厨房先做
  -> 你点了别的
  -> 预做的菜丢弃，不影响正式订单
```

映射到 Agent：

```text
Agent 回复用户
  -> 用户正在阅读和思考
  -> 后台 fork agent 预测用户下一步
  -> 在 overlay 里提前执行安全操作
  -> 用户下一步输入与预测匹配
  -> 直接提交或复用推测结果
```

## 3. 当前问题

当前 gptme 的会话节奏是严格串行的：

```text
Agent 回复
  -> 等用户阅读、思考、输入、回车
  -> Agent 收到下一条 user message
  -> 再调用模型
  -> 再执行工具
  -> 再返回结果
```

这会造成一个明显的空闲窗口：

```text
Agent 已经回复完
用户还没输入下一句话
  -> 模型空闲
  -> 工具系统空闲
  -> 本地 CPU / IO 空闲
  -> 可能本可以提前做的 read、rg、测试、轻量分析没有做
```

当前系统缺少四类能力：

```text
预测缺口：
  - Agent 不会根据当前上下文预测用户下一步可能问什么或要求做什么。
  - 没有把预测结果变成可执行 speculative prompt 的机制。

后台执行缺口：
  - 没有 fork agent 在用户输入前先跑。
  - 没有把后台执行和主会话状态隔离。

Copy-on-Write 文件隔离缺口：
  - 写文件工具默认作用于真实 workspace。
  - 缺少 overlay 读写层：读优先看 overlay，写只写 overlay。
  - 没有确认后把 overlay 提交回主目录、猜错后删除 overlay 的统一流程。

边界控制缺口：
  - 推测执行不能赌危险操作。
  - 遇到需要用户确认的 bash、未知工具、联网、删除、提交、推送等操作时，必须精确暂停。
```

## 4. 目标

实现第一版推测性执行加速框架，让 Agent 在回复用户后，可以利用用户输入前的空档做安全的后台预测和预执行。

目标流程：

```text
主 Agent 回复用户
  -> SpeculationManager 捕获当前会话快照
  -> Predictor 生成用户下一步可能输入
  -> ForkAgent 基于预测 prompt 启动后台执行
  -> OverlayWorkspace 接管文件读写
  -> PolicyGuard 控制危险工具边界
  -> 推测结果进入 waiting_confirmation
  -> 用户下一条输入到达
  -> 匹配预测：提交 overlay 或复用结果
  -> 不匹配预测：丢弃 overlay 和后台状态
```

本阶段要达成五个核心能力：

1. 能在 Agent 回复后启动一个或多个后台推测分支。
2. 能预测用户下一步输入，并把预测变成可执行的 speculative prompt。
3. 能用 Copy-on-Write overlay 隔离文件写入，确保推测执行不污染主工作区。
4. 能在危险操作、需确认操作或未知工具处暂停，不在后台越权执行。
5. 能在用户真实输入到达后确认命中或丢弃推测结果，并支持继续预测下一步形成流水线。

## 5. 功能需求

### 5.1 新增 Speculation 模块

建议新增目录：

```text
gptme/speculation/
  __init__.py
  types.py
  manager.py
  predictor.py
  fork_agent.py
  overlay.py
  matcher.py
  commit.py
  audit.py
```

与现有 subagent 的关系：

```text
gptme 已经有 subagent 执行后端，因此 Task 3 不重新实现一套 Agent runtime。
fork_agent.py 的定位是 speculation 专用封装层：
  - 复用现有 subagent 的线程/子进程/日志目录/并发控制能力。
  - 额外补上会话快照、预测 prompt、overlay workspace、匹配、提交/丢弃和指标。
  - 现有 subagent 默认不完整继承父会话历史，speculation 层需要显式构造 messages_snapshot。
```

核心入口可以设计为：

```text
start_speculation(context: SpeculationContext) -> SpeculationRun
resolve_speculation(user_msg: Message, active_runs: list[SpeculationRun]) -> SpeculationResolution
commit_overlay(run: SpeculationRun) -> CommitResult
discard_overlay(run: SpeculationRun) -> DiscardResult
```

第一版可以先提供内部 API 和测试，不强制默认启用。

### 5.2 核心数据结构

`SpeculationContext` 至少包含：

```text
conversation_id: str
logdir: Path
workspace: Path
messages_snapshot: list[Message]
last_assistant_message: Message
available_tools: list[str]
policy_mode: str
max_predictions: int
```

`PredictedPrompt` 至少包含：

```text
prediction_id: str
content: str
confidence: float
reason: str
allowed_tools_hint: list[str]
expires_at: datetime
```

`SpeculationRun` 至少包含：

```text
run_id: str
prediction: PredictedPrompt
status: running / waiting_confirmation / matched / discarded / blocked / failed
overlay_root: Path
fork_logdir: Path
messages_snapshot_hash: str
tool_events: list[SpeculativeToolEvent]
policy_decisions: list[PolicyDecision]
result_messages: list[Message]
created_at: datetime
finished_at: datetime | None
```

`SpeculationResolution` 至少包含：

```text
user_msg: Message
matched_run_id: str | None
match_score: float
action: commit / reuse_readonly_result / discard_all / ask_user
reason: str
```

### 5.3 预测阶段

预测发生在主 Agent 已经回复用户之后、下一条用户输入到达之前。

预测输入：

```text
最近用户意图
刚刚的 assistant 回复
未完成任务状态
工具结果摘要
当前 workspace 摘要
PolicyGuard 审计结果
```

预测输出是若干条 `PredictedPrompt`，例如：

```text
用户当前问：“这个 PolicyGuard 怎么测试？”
assistant 已经解释完第一阶段测试。

预测 1：“帮我补第二阶段测试。”
预测 2：“把这些内容写进 Notion。”
预测 3：“继续实现 task3。”
```

第一版要求：

1. 最多生成 1 到 3 个预测。
2. 每个预测必须有置信度和简短理由。
3. 低置信度预测不启动执行。
4. 预测 prompt 不能凭空扩大用户授权边界。
5. 预测结果必须有 TTL，过期后自动丢弃。

`TTL` 是 Time To Live，中文是“存活时间”。这里表示推测结果最多保留多久。

### 5.4 Fork Agent 后台执行

`fork agent` 使用当前会话快照启动后台执行，但不能直接写入主会话日志。

执行要求：

1. fork agent 读取 `messages_snapshot`，不能读取用户尚未发送的真实下一条消息。
2. fork agent 的日志写入 `fork_logdir`，不能写入主会话的 `conversation.jsonl`。
3. fork agent 使用相同工具系统，但工具执行必须经过 overlay 和 PolicyGuard。
4. fork agent 的输出先保存为 `result_messages`，只有命中预测后才允许进入主会话。
5. 一个主会话最多同时运行有限数量的 fork agent，默认 1 个。
6. 当用户真实输入到达时，应取消仍在运行且明显不匹配的 fork agent。

第一版可以先实现“单 fork agent”，后续再扩展多预测并行。

### 5.5 Copy-on-Write Overlay 文件隔离

所有推测执行中的文件写操作必须重定向到：

```text
/tmp/speculation/<pid>/<run_id>/
```

其中 `<pid>` 是当前 gptme 进程 ID，`<run_id>` 是本次推测执行 ID。

Overlay 读写规则：

```text
read(path):
  如果 overlay 中存在 path 的修改版本
    -> 读 overlay 文件
  否则
    -> 读真实 workspace 文件

write(path, content):
  不写真实 workspace
  -> 写 /tmp/speculation/<pid>/<run_id>/<relative_path>

delete(path):
  不删除真实 workspace
  -> 在 overlay 中写入 tombstone 删除标记

listdir(path):
  合并真实 workspace 目录和 overlay 目录
  overlay 新增文件可见
  tombstone 标记的文件不可见
```

`tombstone` 是删除标记。它表示“在 overlay 视图里这个文件已删除”，但真实 workspace 中的文件还没有被删除。

第一版重点覆盖这些工具：

```text
read
save
append
patch
patch_many
shell
python / ipython
```

第一批可以先接入 `read`、`save`、`append`，验证 overlay 读写隔离正确后，
再处理 `patch`、`patch_many`。补丁工具还涉及确认预览和补丁匹配，应避免和
基础 overlay 改造混在同一个风险面里。

实现上可以先做工具层 overlay：

1. 文件读写工具通过 `OverlayWorkspace.resolve_read()` 和 `resolve_write()` 访问路径。
2. shell 命令默认在 overlay materialized workspace 中运行，或通过受控 cwd 让文件副作用进入 overlay。
3. Python/IPython 的当前工作目录指向 overlay 视图，避免直接改真实 workspace。

第一版不要求实现内核级 overlayfs，但要保证 gptme 工具层面的文件副作用不会直接落到主工作区。

### 5.6 危险边界控制

推测执行必须比正常执行更保守。不能因为是后台推测，就自动执行危险操作。

遇到以下情况必须暂停或阻止：

```text
PolicyGuard 返回 deny
PolicyGuard 返回 ask，且需要用户显式确认
未知工具
联网下载或上传
git commit / push / reset / clean
rm / mv / chmod / chown 等破坏性 shell
访问 .env、SSH key、API key、凭据文件
修改 workspace 外路径
启动长时间后台进程
```

暂停状态：

```text
status = waiting_confirmation
reason = "requires explicit confirmation"
```

此时 fork agent 不继续向下执行危险工具。等用户真实输入到达后，如果用户明确确认该操作，才可以在主会话中继续，不在后台自动赌。

### 5.7 确认、提交与丢弃

当用户下一条真实输入到达时，需要判断它是否命中某个预测。

匹配依据：

```text
预测 prompt 与真实 user message 的语义相似度
真实 user message 是否授权同类操作
工具调用目标是否一致
文件路径和任务范围是否一致
```

匹配结果：

```text
高匹配 + 只读结果:
  可以直接复用 fork agent 的 result_messages。

高匹配 + overlay 写入:
  展示 overlay diff。
  用户确认后 commit overlay 到主工作区。

低匹配:
  discard overlay。
  删除 fork_logdir 或标记为 discarded。
  正常处理用户输入。

边界不清:
  ask_user，要求用户确认是否采用推测结果。
```

提交 overlay 时必须满足：

1. 用户真实输入与预测高匹配。
2. 用户授权范围覆盖 overlay 中的文件修改。
3. PolicyGuard 没有 deny。
4. 如果 overlay 涉及写文件，必须展示 diff。
5. 主工作区相关文件自推测开始后没有被外部修改；如果已变化，停止提交。

丢弃 overlay 时必须：

1. 删除 `/tmp/speculation/<pid>/<run_id>/`。
2. 取消仍在运行的 fork agent。
3. 审计日志记录丢弃原因。
4. 不向主 `conversation.jsonl` 注入 fork agent 结果。

### 5.8 套娃预测和流水线

当一次推测执行完成后，可以继续基于 fork agent 的结果预测下一步，形成流水线：

```text
主 Agent 回复 A
  -> 预测用户会问 B
  -> fork agent 执行 B
  -> fork agent 回复 B'
  -> 再预测用户会问 C
  -> fork agent 执行 C
```

第一版只要求保留接口，不要求默认开启无限递归。

限制要求：

1. 默认最大 speculation depth 为 1。
2. 可配置最大 depth，例如 2。
3. 每层必须有 TTL。
4. 总 token、总执行时间、overlay 大小和 fork agent 数量必须有限制。
5. 任意一层遇到危险边界，停止继续套娃预测。

### 5.9 审计日志

新增 speculation 级别审计日志，建议记录到当前 logdir：

```text
speculation-events.jsonl
```

每次推测执行至少记录：

```text
run_id
conversation_id
prediction_id
predicted_prompt
prediction_confidence
prediction_reason
overlay_root
fork_logdir
status
started_at
finished_at
tool_events
policy_decisions
overlay_changed_files
overlay_diff_summary
matched_user_message
match_score
resolution_action
discard_reason
commit_result
```

审计日志要能回答面试里的三个问题：

1. Agent 为什么预测用户下一步会这么问？
2. fork agent 在后台提前做了什么？
3. 真实用户输入到达后，为什么提交、复用、暂停或丢弃推测结果？

### 5.10 配置和启动参数

建议支持环境变量：

```text
GPTME_SPECULATION_MODE=off|manual|auto
GPTME_SPECULATION_MAX_RUNS=1
GPTME_SPECULATION_MAX_DEPTH=1
GPTME_SPECULATION_WARMUP_TURNS=3
GPTME_SPECULATION_TTL_SECONDS=120
GPTME_SPECULATION_OVERLAY_ROOT=/tmp/speculation
GPTME_SPECULATION_MIN_CONFIDENCE=0.75
GPTME_SPECULATION_ALLOW_WRITES=0|1
GPTME_SPECULATION_METRICS=0|1
```

模式含义：

```text
off:
  不启用推测性执行。

manual:
  只在用户或代码显式调用时启动推测执行。

auto:
  主 Agent 每次回复后自动尝试预测和 fork。
```

第一版建议默认 `off` 或 `manual`，避免改变现有用户体验。

`GPTME_SPECULATION_WARMUP_TURNS` 表示会话预热轮数。只有主会话完成至少 N 轮用户请求后，才允许自动启动推测执行。原因是会话刚开始时主题还不稳定，Agent 对用户下一步意图掌握的信息少，推测命中率低，容易浪费 token、工具调用和本地 IO。随着会话变长，主题、代码范围、用户偏好和任务目标更明确，预测才更有价值。

自动启动推测执行需要同时满足：

```text
GPTME_SPECULATION_MODE=auto
已完成 turn 数 >= GPTME_SPECULATION_WARMUP_TURNS
预测 confidence >= GPTME_SPECULATION_MIN_CONFIDENCE
未超过 GPTME_SPECULATION_MAX_RUNS
未超过 GPTME_SPECULATION_MAX_DEPTH
当前没有等待用户确认的危险推测分支
```

`turn` 是一轮用户请求。这里指从用户输入开始，到 Agent 完成这轮回复结束。

### 5.11 性能指标与对比实验

推测性执行必须能作为一个开关启用或关闭，方便在长会话中对比它对 Agent 回复速度和吞吐率的影响。

至少记录这些指标：

```text
turn_id
speculation_mode
prediction_started_at
prediction_finished_at
fork_started_at
fork_finished_at
user_message_arrived_at
normal_execution_started_at
normal_execution_finished_at
speculation_hit: true / false
speculation_reused: true / false
speculation_committed: true / false
speculation_discarded: true / false
time_saved_ms
overlay_bytes_written
tool_calls_preexecuted
```

对比方式：

```text
baseline:
  GPTME_SPECULATION_MODE=off
  正常串行会话。

experiment:
  GPTME_SPECULATION_MODE=auto
  主 Agent 回复后自动预测和 fork。
```

长会话中重点比较：

1. 用户下一条输入到达后的等待时间是否降低。
2. 完成同一组多轮任务的总耗时是否降低。
3. fork agent 预执行命中率是多少。
4. 猜错导致的额外 token、工具调用和 overlay 写入成本是多少。
5. 推测执行是否增加了危险操作确认或误提交风险。
6. 不同 `GPTME_SPECULATION_WARMUP_TURNS` 下，命中率和节省时间如何变化。

这些指标可以写入 `speculation-events.jsonl`，也可以单独写入：

```text
speculation-metrics.jsonl
```

## 6. 与 PolicyGuard 的关系

`PolicyGuard` 是推测执行的安全边界。Speculation 不能绕过它。

关系如下：

```text
ForkAgent 生成工具调用
  -> OverlayWorkspace 重写文件读写路径
  -> PolicyGuard.evaluate_tool_use()
  -> allow: 可以在 overlay 中执行
  -> ask: 后台暂停，等待真实用户确认
  -> deny: 阻止推测分支
```

关键原则：

1. 推测执行只允许赌低风险、可丢弃、可隔离的操作。
2. 需要用户确认的操作不能在后台自动确认。
3. 真实提交 overlay 前还要再次检查用户授权范围。
4. PolicyGuard 的审计结果必须写入 speculation 审计日志。

## 7. 非目标

本任务只实现第一版推测执行加速框架。

暂不实现：

1. 内核级 overlayfs 或 FUSE 文件系统。
2. 完整进程级 sandbox 或容器隔离。
3. 多用户共享 speculation cache。
4. 跨机器远程推测执行。
5. 复杂多分支长期并行搜索。
6. 在后台自动执行需要用户确认的危险操作。
7. 未经用户确认把 overlay 写回主工作区。
8. 自动 git commit、push 或 reset。

## 8. 验收标准

1. 主 Agent 回复后，可以启动一个 fork agent 预测下一条用户输入。

2. 预测结果包含 `PredictedPrompt.content`、`confidence`、`reason` 和 TTL。

3. fork agent 的日志写入独立 `fork_logdir`，不会污染主 `conversation.jsonl`。

4. 推测执行中的 `save`、`append`、`patch` 写入 overlay，不修改真实 workspace。

5. 读文件时，如果 overlay 中存在修改版本，优先返回 overlay 内容。

6. 删除文件时只产生 overlay tombstone，不删除真实文件。

7. PolicyGuard 返回 `ask` 或 `deny` 时，fork agent 不继续执行危险工具。

8. 用户真实输入与预测高匹配时，可以复用只读结果，或展示 overlay diff 后提交写入。

9. 用户真实输入与预测不匹配时，overlay 被删除，fork agent 结果不会进入主会话。

10. 提交 overlay 前，如果主工作区相关文件已变化，提交必须失败。

11. 每次推测执行都有 `speculation-events.jsonl` 审计记录。

12. `GPTME_SPECULATION_MODE=off` 时，默认配置下不改变现有 gptme 普通会话行为。

13. `GPTME_SPECULATION_MODE=auto` 时，主 Agent 回复后可以自动启动推测分支。

14. 开启 `GPTME_SPECULATION_METRICS=1` 后，可以记录命中率、节省时间、额外成本等长会话对比指标。

15. 已完成 turn 数小于 `GPTME_SPECULATION_WARMUP_TURNS` 时，即使 mode 是 `auto`，也不会启动推测分支。

## 9. 建议测试

1. `Predictor` 能基于最后一条 assistant 回复生成 `PredictedPrompt`。

2. 低置信度预测不会启动 fork agent。

3. fork agent 写入的消息只进入 `fork_logdir`，不进入主 logdir。

4. `OverlayWorkspace.write()` 写入 `/tmp/speculation/<pid>/<run_id>/...`，真实文件不变。

5. `OverlayWorkspace.read()` 对已修改文件优先读取 overlay。

6. `OverlayWorkspace.delete()` 生成 tombstone，真实文件仍存在。

7. `save` / `append` / `patch` 在 speculation 模式下只改 overlay。

8. `shell` 在 speculation 模式下的文件副作用不会落到真实 workspace。

9. PolicyGuard 返回 `ask` 时，`SpeculationRun.status == waiting_confirmation`。

10. 用户输入与预测匹配时，`resolve_speculation()` 返回 `commit` 或 `reuse_readonly_result`。

11. 用户输入与预测不匹配时，`resolve_speculation()` 返回 `discard_all`。

12. `commit_overlay()` 能把 overlay diff 应用到主 workspace。

13. 主 workspace 文件在推测期间被外部修改时，`commit_overlay()` 拒绝提交。

14. 丢弃推测结果后，overlay 目录被删除。

15. `GPTME_SPECULATION_MAX_DEPTH=1` 时，不会继续套娃预测第二层。

16. 审计日志包含 prediction、overlay、PolicyGuard 决策、match 结果和最终处理动作。

17. `GPTME_SPECULATION_MODE=off` 时不会启动 predictor、fork agent 或 overlay。

18. `GPTME_SPECULATION_METRICS=1` 时会写入 speculation metrics，并包含 `time_saved_ms` 和 `speculation_hit`。

19. `GPTME_SPECULATION_WARMUP_TURNS=3` 时，前两轮用户请求结束后不会自动推测，第三轮结束后才允许进入预测逻辑。

## 10. 推荐实现顺序

1. 实现 `types.py`，定义预测、运行、overlay、匹配和提交结果。

2. 实现 `overlay.py`，先完成工具层 Copy-on-Write 文件读写。

3. 为 `read`、`save`、`append`、`patch` 接入 overlay 路径解析。

4. 实现 `predictor.py`，先用可测试的本地规则或 mock LLM 生成预测。

5. 实现 `fork_agent.py`，让后台分支使用独立 logdir 和 overlay workspace。

6. 实现 `manager.py`，在主 Agent 回复后启动 speculation run。

7. 实现 `matcher.py`，判断真实 user message 是否命中预测。

8. 实现 `commit.py`，支持展示 diff、提交 overlay、丢弃 overlay。

9. 接入 PolicyGuard：allow 执行，ask 暂停，deny 阻止。

10. 添加 `speculation-events.jsonl` 审计日志。

11. 补完整单元测试，再考虑默认启用或接入 CLI 参数。

第一版可以不追求预测非常准，重点是把“预测 -> fork -> overlay 执行 -> 确认/丢弃”这条链路做对。

## 11. 简历表达方向

实现第三阶段后，可以表述为：

```text
在 gptme 本地 Agent runtime 中实现 AI 版推测性执行：主 Agent 回复后，后台 fork agent 预测用户下一步输入，并在 Copy-on-Write overlay 中提前执行低风险工具调用；真实用户输入到达后，如果命中预测则复用结果或提交 overlay，否则丢弃分支，从而降低多轮交互等待时间并提高吞吐率。
```

面试中可以强调三个技术点：

1. 它不是普通的“候选方案评测”，而是利用用户思考时间做分支预测和后台预执行。
2. Copy-on-Write overlay 让后台写操作可提交、可丢弃、可审计，不污染真实 workspace。
3. PolicyGuard 负责边界控制：低风险操作可以提前做，危险操作必须停在确认点。
