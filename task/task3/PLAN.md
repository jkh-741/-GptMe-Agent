# Task 3 实现计划：Speculative Execution 推测性执行加速

## 目标

在 gptme 的多轮 Agent 会话中加入可开关的推测性执行机制：主 Agent 回复后，后台 fork agent 预测用户下一步输入，并在 Copy-on-Write overlay 中提前执行低风险工具调用。真实用户输入到达后，如果命中预测，就复用结果或提交 overlay；如果没有命中，就丢弃推测分支。

目标调用链：

```text
主 Agent 完成一轮回复
  -> SpeculationManager 检查 GPTME_SPECULATION_MODE
  -> Predictor 预测用户下一步输入
  -> ForkAgent 使用会话快照后台执行
  -> OverlayWorkspace 接管读写
  -> PolicyGuard 控制 allow / ask / deny
  -> 用户真实输入到达
  -> Matcher 判断是否命中预测
  -> 命中：复用只读结果或提交 overlay
  -> 未命中：丢弃 overlay 和 fork 状态
  -> Metrics 记录命中率、节省时间和额外成本
```

第一版重点不是预测准确率，而是把这条工程链路做对：可开关、可隔离、可暂停、可丢弃、可审计、可度量。

## 实现步骤

### 1. 梳理主会话可插入点

重点阅读：

```text
gptme/chat.py::chat()
gptme/chat.py::_run_chat_loop()
gptme/chat.py::_process_message_conversation()
gptme/chat.py::step()
gptme/logmanager/
gptme/tools/__init__.py::execute_msg()
gptme/tools/base.py::ToolUse.execute()
gptme/policyguard/evaluator.py::evaluate_tool_use()
```

需要确认四件事：

```text
主 Agent 什么时候完成一次 assistant 回复
用户下一条输入在哪里被读取
如何复制当前 messages snapshot
工具执行时如何把 workspace 切换为 overlay 视图
```

第一版建议先不要深改主 loop。先做内部 API 和单元测试，再用最小 hook 接入。

### 2. 新增 speculation 包和核心类型

新增目录：

```text
gptme/speculation/
  __init__.py
  types.py
  overlay.py
  predictor.py
  fork_agent.py
  matcher.py
  commit.py
  manager.py
  audit.py
  metrics.py
```

`fork_agent.py` 不重新实现新的 Agent runtime，而是作为现有
`gptme.tools.subagent` 后端之上的推测执行封装层。已有 subagent 提供线程、
子进程、独立日志、并发控制和 profile 能力；Task 3 需要补的是父会话
`messages_snapshot`、预测 prompt、overlay workspace、匹配、提交/丢弃和指标。

核心类型：

```text
SpeculationMode: off / manual / auto
SpeculationStatus: running / waiting_confirmation / matched / discarded / blocked / failed
ResolutionAction: commit / reuse_readonly_result / discard_all / ask_user
SpeculationContext
PredictedPrompt
SpeculationRun
SpeculativeToolEvent
SpeculationResolution
CommitResult
DiscardResult
SpeculationMetrics
```

这里先把数据结构定义清楚，后续模块只围绕这些对象流转。

### 3. 实现配置开关

实现统一配置读取：

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

要求：

```text
off:
  不启动 predictor、fork agent、overlay。

manual:
  只允许测试或显式 API 调用启动 speculation。

auto:
  主 Agent 回复后自动尝试 prediction + fork。
```

第一版默认必须是 `off`，避免影响现有 gptme 行为。

自动启动还需要经过 warm-up gate：

```text
completed_turns >= GPTME_SPECULATION_WARMUP_TURNS
prediction_confidence >= GPTME_SPECULATION_MIN_CONFIDENCE
active_runs < GPTME_SPECULATION_MAX_RUNS
depth < GPTME_SPECULATION_MAX_DEPTH
```

`GPTME_SPECULATION_WARMUP_TURNS` 用来避免会话刚开始时盲目推测。前几轮上下文少、主题不稳定，推测更容易 miss；等用户和 Agent 已经围绕明确任务连续交流几轮后，再启动推测更容易命中，也更适合做吞吐率优化。

### 4. 实现 Copy-on-Write OverlayWorkspace

新增：

```text
gptme/speculation/overlay.py
```

核心接口：

```text
class OverlayWorkspace:
    def read_path(path: Path) -> Path
    def write_path(path: Path) -> Path
    def delete_path(path: Path) -> None
    def listdir(path: Path) -> list[Path]
    def changed_files() -> list[Path]
    def diff_summary() -> str
```

读写规则：

```text
read:
  overlay 有修改版本 -> 读 overlay
  否则 -> 读真实 workspace

write:
  写 /tmp/speculation/<pid>/<run_id>/<relative_path>
  不写真实 workspace

delete:
  写 tombstone
  不删真实 workspace
```

第一版先实现工具层 overlay，不做内核级 overlayfs 或 FUSE。

### 5. 先接入文件类工具

优先接入：

```text
read
save
append
patch
patch_many
```

第一批先接入 `read`、`save`、`append`，验证 overlay 读写不污染真实 workspace；
`patch` 和 `patch_many` 放到下一批，因为它们还要处理确认预览和补丁匹配。

实现方式：

```text
正常模式:
  工具按原逻辑读写 workspace。

speculation 模式:
  工具从当前 ContextVar 或执行上下文中拿 OverlayWorkspace。
  读路径走 overlay.read_path()。
  写路径走 overlay.write_path()。
```

这一阶段只要求文件工具副作用不落到真实 workspace。

### 6. 再接入 shell 和 python

shell 和 python 更难，因为它们可以产生任意文件副作用。

第一版保守策略：

```text
shell:
  只允许低风险只读命令在 speculation 中执行。
  写文件、删除、重定向、联网、git mutation 命令直接 waiting_confirmation 或 blocked。

python / ipython:
  只允许无文件写入、无 subprocess、无网络、无危险 import 的低风险代码。
  命中 AST 风险时暂停。
```

如果后续要支持 shell 写入，可以再做 materialized overlay workspace；第一版不必为任意 shell 副作用兜底。

### 7. 实现 Predictor

新增：

```text
gptme/speculation/predictor.py
```

接口：

```text
predict_next_prompts(context: SpeculationContext) -> list[PredictedPrompt]
```

第一版可分两层：

```text
HeuristicPredictor:
  本地规则，便于测试。

ModelBackedPredictor:
  后续接 LLM，根据会话上下文预测下一步。
```

预测必须满足：

```text
有 confidence
有 reason
有 expires_at
低于 GPTME_SPECULATION_MIN_CONFIDENCE 不启动 fork
不能扩大用户授权边界
```

### 8. 实现 ForkAgent

新增：

```text
gptme/speculation/fork_agent.py
```

职责：

```text
复制 messages_snapshot
创建 fork_logdir
创建 OverlayWorkspace
封装现有 gptme.tools.subagent 后端，而不是重写 Agent runtime
用 predicted prompt 驱动一次或多次 step
收集 result_messages
收集 tool_events 和 PolicyDecision
```

第一版建议先实现同步版本：

```text
run_fork_once(context, prediction) -> SpeculationRun
```

再扩展后台线程或 asyncio task。这样单元测试更稳定。

### 9. 接入 PolicyGuard 边界

fork agent 执行工具前仍然进入：

```text
PolicyGuard.evaluate_tool_use(...)
```

策略：

```text
allow:
  可以在 overlay 或只读模式中执行。

ask:
  SpeculationRun.status = waiting_confirmation
  停止继续执行。

deny:
  SpeculationRun.status = blocked
  不执行工具。
```

推测执行不允许 no-confirm 或 auto-confirm 绕过 `ask`。

### 10. 实现 Matcher

新增：

```text
gptme/speculation/matcher.py
```

接口：

```text
resolve_speculation(user_msg, active_runs) -> SpeculationResolution
```

第一版先用可测试规则：

```text
关键词重合
路径重合
工具意图一致
预测 prompt 与真实输入相似度超过阈值
```

后续可以替换为 LLM matcher 或 embedding matcher。

结果：

```text
高匹配 + 只读结果 -> reuse_readonly_result
高匹配 + overlay 写入 -> ask_user 或 commit
低匹配 -> discard_all
边界不清 -> ask_user
```

### 11. 实现 overlay commit / discard

新增：

```text
gptme/speculation/commit.py
```

提交前检查：

```text
用户真实输入与预测匹配
用户授权范围覆盖 overlay 修改
PolicyGuard 没有 deny
overlay diff 已展示
主 workspace 文件自 speculation 开始后没有变化
```

丢弃要求：

```text
删除 overlay 目录
取消 fork agent
不向主 conversation.jsonl 注入 fork 结果
记录 discard reason
```

第一版可以先只支持文件覆盖提交，不做复杂 merge。

### 12. 实现审计日志

新增：

```text
gptme/speculation/audit.py
```

写入：

```text
<logdir>/speculation-events.jsonl
```

记录：

```text
run_id
prediction
confidence
overlay_root
fork_logdir
tool_events
policy_decisions
overlay_changed_files
match_score
resolution_action
commit_or_discard_result
```

审计日志用于解释“为什么预测、提前做了什么、最后为什么提交或丢弃”。

### 13. 实现性能指标

新增：

```text
gptme/speculation/metrics.py
```

开启条件：

```text
GPTME_SPECULATION_METRICS=1
```

写入：

```text
<logdir>/speculation-metrics.jsonl
```

指标：

```text
speculation_mode
warmup_turns
completed_turns_before_speculation
prediction_started_at
prediction_finished_at
fork_started_at
fork_finished_at
user_message_arrived_at
normal_execution_started_at
normal_execution_finished_at
speculation_hit
speculation_reused
speculation_committed
speculation_discarded
time_saved_ms
overlay_bytes_written
tool_calls_preexecuted
```

这个模块是为了做长会话 A/B 对比：

```text
baseline: GPTME_SPECULATION_MODE=off
experiment: GPTME_SPECULATION_MODE=auto
```

重点比较用户输入后的等待时间、整段长会话耗时、命中率和猜错成本。
同时比较不同 warm-up 配置：

```text
GPTME_SPECULATION_WARMUP_TURNS=0
GPTME_SPECULATION_WARMUP_TURNS=3
GPTME_SPECULATION_WARMUP_TURNS=5
```

观察会话早期 miss 成本是否下降，以及长会话中 time_saved_ms 是否仍然明显。

### 14. 最小接入主 Agent loop

第一版接入点：

```text
主 Agent 完成 assistant 回复后:
  如果 mode == auto
    -> 检查 warm-up gate
    -> start_speculation()

用户下一条输入到达后:
  如果存在 active speculation
    -> resolve_speculation()
    -> commit / reuse / discard
```

注意：

```text
off 模式必须完全不改变现有逻辑。
manual 模式只暴露 API，不自动启动。
auto 模式通过 warm-up gate 后才自动预测和 fork。
```

### 15. 测试计划

优先写单元测试：

```text
tests/test_speculation_overlay.py
tests/test_speculation_predictor.py
tests/test_speculation_matcher.py
tests/test_speculation_commit.py
tests/test_speculation_manager.py
```

核心覆盖：

```text
off 模式不启动 speculation
auto 模式在 warm-up turn 不足时不启动 speculation
low confidence 不启动 fork
fork logdir 不污染主 logdir
overlay read/write/delete/tombstone
文件工具只写 overlay
PolicyGuard ask/deny 让 fork 暂停或阻止
真实输入匹配时复用或提交
真实输入不匹配时丢弃
主文件变化时拒绝 commit
metrics 记录 hit、discard、time_saved_ms
metrics 记录 warmup_turns 和 completed_turns_before_speculation
```

再补少量集成测试：

```text
长会话模拟：off vs auto
warm-up=0 vs warm-up=3 的命中率和资源浪费对比
auto 模式下命中预测时减少用户输入后的等待
auto 模式下猜错时不会污染 workspace
```

### 16. 实现优先级

建议分三期：

```text
Phase 1: 可测基础设施
  types + config + overlay + audit + metrics

Phase 2: 单分支推测链路
  predictor + fork_agent + PolicyGuard boundary + matcher

Phase 3: 主 loop 接入和性能对比
  auto/off/manual 开关 + commit/discard + 长会话 metrics
```

第一版完成标准：

```text
开关可控
overlay 安全
fork 可丢弃
PolicyGuard 可暂停危险操作
能记录 A/B 对比指标
```
