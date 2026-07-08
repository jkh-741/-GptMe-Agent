# Task 2 第二阶段交接说明：Fast / Thinking 语义审查骨架

本文档记录当前终端会话中已经完成的第二阶段改动，方便后续 Agent 接手。

## 当前结论

第二阶段目前完成的是“真实模型接入前的可测试骨架”，还没有连接 DeepSeek API。

已经做到：

```text
Fast / Thinking 两阶段语义审查的调度结构已写出
Fast / Thinking 结果可以分别记录到 PolicyDecision
语义审查支持可注入 judge，用于测试模拟模型返回
JSON 输出解析和非法 JSON fallback 已实现
Thinking 的 action_hint=deny 可以影响最终 PolicyDecision
审计日志已能记录 fast_semantic_result 和 thinking_semantic_result
新增 mock 测试，不会真实访问外部模型
```

还没有做到：

```text
没有真实调用 gptme.llm.reply() 或 DeepSeek API
没有新增 direct deepseek-v4-flash / deepseek-v4-pro 模型元数据
没有实现真正的 prompt 构造和敏感内容 redaction
没有实现 LLM timeout 参数
没有把 semantic latency、fallback_used、semantic_error 等增强字段完整写入审计日志
没有跑全量 make test
```

## 已修改文件

### gptme/policyguard/types.py

改动点：

```text
SemanticRiskResult 新增字段：
  action_hint: PolicyAction | None
  model: str | None
  error: str | None

PolicyDecision 新增字段：
  fast_semantic_result: SemanticRiskResult | None
  thinking_semantic_result: SemanticRiskResult | None
```

目的：

```text
第一阶段只有一个 semantic_result，无法表达 Fast 和 Thinking 两个阶段分别发生了什么。
新增字段后，最终决策仍使用 semantic_result，但审计和测试可以看到 Fast / Thinking 的独立结果。
```

### gptme/policyguard/semantic.py

改动点：

```text
新增 DEFAULT_FAST_MODEL = "deepseek/deepseek-v4-flash"
新增 DEFAULT_THINKING_MODEL = "deepseek/deepseek-v4-pro"
新增 SemanticJudge 类型别名
新增 set_semantic_judge_for_testing()
新增 build_semantic_judge_payload()
新增 parse_semantic_judge_response()
新增 ModelBackedSemanticClassifier
FastSemanticClassifier / ThinkingSemanticClassifier 继承 ModelBackedSemanticClassifier
```

当前行为：

```text
默认情况下 _semantic_judge 为 None，所以不会调用真实模型。
没有 judge 时，Fast / Thinking 仍使用 HeuristicSemanticClassifier 的结果作为 fallback。
测试可以通过 set_semantic_judge_for_testing() 注入一个假的 judge。
假的 judge 返回 JSON 字符串，parse_semantic_judge_response() 会解析为 SemanticRiskResult。
如果 judge 抛异常或返回非法 JSON，会退回 heuristic，并在 SemanticRiskResult.error 中记录错误。
```

注意：

```text
set_semantic_judge_for_testing() 目前是测试 hook，不是最终生产 API。
后续真实 DeepSeek 接入时，应新增生产用 LLM judge 函数，而不是让测试 hook 承担生产职责。
```

### gptme/policyguard/evaluator.py

改动点：

```text
_run_semantic_checks() 返回三元组：
  final_semantic_result
  fast_semantic_result
  thinking_semantic_result

mode=off:
  只返回 heuristic，fast/thinking 为 None。

mode=fast:
  只运行 Fast，final=fast。

mode=thinking:
  只运行 Thinking，final=thinking。

mode=both:
  先运行 Fast。
  如果 Fast requires_thinking、Fast verdict 不是 allow、Fast confidence < 0.7，
  或 static risk >= medium，则运行 Thinking。
  如果运行 Thinking，final=thinking；否则 final=fast。
```

`merge_policy_results()` 新增参数：

```text
fast_semantic_result
thinking_semantic_result
```

并且支持：

```text
semantic_result.action_hint == deny -> PolicyAction.DENY
semantic_result.action_hint == ask -> PolicyAction.ASK
```

目的：

```text
Thinking 模型可以通过 action_hint 更明确地影响最终 allow / ask / deny 决策。
```

### gptme/policyguard/audit.py

改动点：

```text
policy-events.jsonl 新增字段：
  fast_semantic_result
  thinking_semantic_result
```

还没做：

```text
semantic_provider
fast_model
thinking_model
semantic_latency_ms
semantic_fallback_used
prompt_redaction_applied
```

这些字段已写入 plan.md，但尚未实现。

### task/task2/plan.md

新增了第 18 节：

```text
第二阶段：接入真实 Fast / Thinking 语义审查
```

内容包括：

```text
heuristic 策略解释
DeepSeek Fast / Thinking 推荐模型
配置项
JSON 输出契约
Prompt 设计
LLM 调用建议
失败降级策略
审计日志增强
第二阶段测试矩阵
```

### tests/test_policyguard_semantic_modes.py

新增测试文件。

覆盖用例：

```text
test_semantic_mode_off_does_not_call_judge
test_fast_mode_uses_injected_fast_judge
test_both_mode_skips_thinking_when_fast_allows_low_static_risk
test_both_mode_runs_thinking_when_static_risk_is_medium
test_thinking_action_hint_deny_blocks_tool_call
test_invalid_judge_json_uses_heuristic_fallback
```

这些测试全部通过 mock judge 完成，不会访问 DeepSeek API。

## 已运行验证

在 Windows 本地运行：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\python.exe -m pytest tests\test_policyguard.py tests\test_policyguard_semantic_modes.py -q
```

结果：

```text
16 passed, 1 warning
```

运行 ruff：

```powershell
.\.venv\python.exe -m ruff check gptme\policyguard tests\test_policyguard_semantic_modes.py
```

结果：

```text
All checks passed!
```

没有运行成功的内容：

```text
make test
```

原因：

```text
当前 Windows 环境的 make 依赖 Unix bash/which，之前运行 make test 时在 Makefile shell 探测阶段失败。
这不是 PolicyGuard 新增测试失败，而是当前 Windows 环境缺少 Unix 工具链。
```

## 跨平台和跨版本评估

当前第二阶段骨架尽量使用 Python 标准库和项目已有模式：

```text
json
os.environ
re
dataclasses.replace
collections.abc.Callable
pytest monkeypatch
```

没有新增 Windows 专用 API，也没有新增 macOS/Linux 专用 API。
新增测试没有使用 `os.getuid()`、`os.set_blocking()` 这类 Unix-only API。

从代码形态看，macOS 上应该可以正常运行定向测试：

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/bin/python -m pytest tests/test_policyguard.py tests/test_policyguard_semantic_modes.py -q
./.venv/bin/python -m ruff check gptme/policyguard tests/test_policyguard_semantic_modes.py
```

但不能承诺“所有版本绝对可用”，原因：

```text
当前只在 Windows + Python 3.11.15 上实际验证。
没有在 macOS 本机实际执行。
没有跑全量 make test。
如果 Mac 上的依赖版本、pytest 插件或 gptme 代码状态不同，仍可能出现环境相关失败。
```

Python 版本方面：

```text
改动使用了 from __future__ import annotations。
类型语法沿用项目现有风格，例如 X | None、list[str]。
这与当前项目 Python 3.11 环境兼容。
如果项目最低版本低于 Python 3.10，则这些类型语法不兼容；但当前本地环境和项目已有代码已经广泛使用该语法。
```

## AGENTS.md 合规情况

本次实现基本按 AGENTS.md 要求执行：

```text
使用了类型注解。
没有引入过度抽象，只做第二阶段需要的最小骨架。
没有真实调用外部模型，测试可离线运行。
没有使用 git add . 或提交。
没有回滚用户已有改动。
使用 apply_patch 修改和新增文件。
新增测试优先用 mock / monkeypatch，避免真实 API 依赖。
定向运行了 pytest 和 ruff check。
```

需要后续继续注意：

```text
真实 DeepSeek 接入时，单元测试仍不能依赖真实 API。
真实 judge prompt 不能把敏感文件正文、API key、token、私钥内容发送给模型。
接入真实 LLM 后需要补充审计字段和失败降级测试。
最终 push 前仍应按 AGENTS.md 跑 make test、make typecheck、make lint；
如果 Windows 环境跑不通 make test，应在 macOS 或具备 bash/poetry/pytest 插件的环境中跑。
```

## 后续接手建议

建议下一位 Agent 按这个顺序继续：

```text
1. 先运行当前定向测试，确认接手环境一致。
2. 在 gptme/llm/models/data.py 中补 direct deepseek-v4-flash 和 deepseek-v4-pro 元数据。
3. 新增生产用 LLM judge，例如 call_semantic_judge_model(stage, model, request)。
4. 构造 Fast / Thinking prompt，并加入敏感信息 redaction。
5. 通过 gptme.llm.reply() 或 _chat_complete() 调用模型，tools=[]，stream=False。
6. 补齐审计字段：模型名、延迟、错误、fallback、redaction。
7. 新增 mock 测试覆盖真实 judge 包装层，不直接打 DeepSeek API。
8. 最后在可用 Unix 工具链环境中跑 make test、make typecheck、make lint。
```
