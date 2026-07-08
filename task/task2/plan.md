# Task 2 实现计划：PolicyGuard 双重安全筛查

## 目标

在 gptme 的工具执行链路中加入统一 `PolicyGuard`，让高风险工具在真正执行前先经过策略判断。

目标调用链：

```text
gptme/chat.py::step()
  -> gptme/tools/__init__.py::execute_msg()
  -> gptme/tools/base.py::ToolUse.execute()
  -> gptme/policyguard/evaluator.py::evaluate_tool_use()
      -> Fast semantic classifier
      -> 必要时 Thinking semantic classifier
      -> 结构化静态检查
      -> 合并语义结果和静态结果
      -> PolicyDecision(allow / ask / deny)
  -> 用户确认或拒绝
  -> 具体工具 execute 函数
```

`PolicyGuard` 是工具执行前的安全门卫。`allow` 是允许执行，`ask` 是要求用户显式确认，`deny` 是拒绝执行。

这次实现要和需求文档保持一致：不是只做 AST、命令和路径的静态规则，而是做“两阶段 LLM-as-Judge 语义审查 + 结构化静态检查”的双层安全边界。第一版可以不强制调用真实 LLM，但代码结构必须为 Fast / Thinking 两阶段语义分类器留好接口。

## 实现步骤

### 1. 梳理现有工具执行入口

重点阅读：

```text
gptme/chat.py::step()
gptme/tools/__init__.py::execute_msg()
gptme/tools/base.py::ToolUse.execute()
gptme/util/ask_execute.py::execute_with_confirmation()
gptme/hooks/confirm.py::get_confirmation()
gptme/tools/shell.py::execute_shell()
gptme/tools/python.py::execute_python()
gptme/tools/patch.py::execute_patch()
gptme/tools/save.py::_validate_and_execute()
gptme/tools/patch_many.py
gptme/tools/morph.py
```

需要确认三件事：

```text
模型生成的工具调用如何从消息文本变成 ToolUse
ToolUse.execute() 如何进入具体工具
现有确认 hook 如何处理 CLI、Server、auto-confirm 和 no-confirm
```

`CLI` 是 Command Line Interface，中文是命令行接口。`Server` 指 WebUI/API 场景里的后端执行模式。

### 2. 新增 PolicyGuard 包和核心类型

新增目录：

```text
gptme/policyguard/
```

先实现：

```text
gptme/policyguard/__init__.py
gptme/policyguard/types.py
gptme/policyguard/evaluator.py
gptme/policyguard/audit.py
```

核心类型：

```text
PolicyAction: allow / ask / deny
RiskLevel: low / medium / high / critical
PolicyCheckResult
SemanticRiskRequest
SemanticRiskResult
StaticRiskResult
PolicyDecision
```

`PolicyDecision` 至少包含：

```text
action
risk_level
reasons
checks
semantic_result
static_result
requires_explicit_confirmation
```

`requires_explicit_confirmation` 很重要：它表示这个 `ask` 不能被 no-confirm 或 auto-confirm 自动绕过。

### 3. 实现工具调用参数归一化

在 `evaluator.py` 中先实现轻量归一化逻辑：

```text
normalize_tool_use(tool_use) -> NormalizedToolUse
```

第一版至少覆盖：

```text
shell: 提取 command
python/ipython: 提取 code
patch: 提取 path + patch content
save/append: 提取 path + content
patch_many: 提取多 path + patch payload
morph: 提取 path + edit instruction
```

归一化的目的：

```text
语义分类器看到统一字段
静态检查器不用重复解析 ToolUse 格式
审计日志可以记录结构化参数
```

这里不要一次性重写所有工具解析逻辑。第一版可以复用现有工具里的 `get_path()`、`patch_many` 解析辅助函数，或者先做保守解析。

### 4. 实现语义检查模块

新增：

```text
gptme/policyguard/semantic.py
```

核心接口：

```text
class SemanticRiskClassifier:
    def classify(request: SemanticRiskRequest) -> SemanticRiskResult
```

实现三个层次：

```text
HeuristicSemanticClassifier
FastSemanticClassifier
ThinkingSemanticClassifier
```

第一版可以让 `FastSemanticClassifier` 和 `ThinkingSemanticClassifier` 先调用本地启发式规则，不发起真实 LLM 请求；但接口、输入输出和模式选择必须按真实 LLM-as-Judge 设计。

`LLM-as-Judge` 是用大语言模型作为评审器，对一次工具调用的意图和风险进行分类。这里 Judge 只判断风险，不执行工具。

### 5. 实现语义检查运行模式

支持配置：

```text
GPTME_POLICYGUARD_SEMANTIC_MODE=off|fast|thinking|both
```

模式含义：

```text
off:
  不调用真实 LLM。
  使用 HeuristicSemanticClassifier。
  CI 和本地测试默认可用。

fast:
  只运行 Fast classifier。
  目标是低延迟快速判断 allow / suspicious / block。

thinking:
  直接运行 Thinking classifier。
  目标是深度复核高风险或复杂工具调用。

both:
  默认目标模式。
  先 Fast，Fast 不确定或高风险时再 Thinking。
```

第一版实现优先级：

```text
先保证 off 模式稳定可测
再实现 fast / thinking / both 的调度逻辑
真实 LLM provider 接入可以放在后续迭代
```

### 6. 定义 Fast / Thinking 的触发规则

Fast classifier 输入：

```text
tool_name
raw_content
normalized_args
workspace
recent_user_intent
assistant_plan_or_message
```

Fast classifier 输出：

```text
verdict: allow / suspicious / block
risk_level
confidence
reasons
requires_thinking
```

触发 Thinking classifier 的条件：

```text
Fast verdict == suspicious
Fast verdict == block 但需要确认是否可由用户授权
Fast confidence 低于阈值
静态检查发现 medium/high/critical 风险
工具是 morph 且目标文件可能包含敏感内容
工具调用和用户意图明显不一致
```

Thinking classifier 输出需要能映射到最终语义判断：

```text
allow / ask / deny
risk_level
reasons
```

### 7. 实现语义层第一版启发式规则

为了避免第一版强依赖真实 LLM，先实现本地启发式语义规则：

```text
用户要求查看状态，但工具要 reset/clean/delete -> deny
用户未明确要求读取凭据，但工具读取 .env/credentials/SSH key -> ask 或 deny
工具会联网下载并执行脚本 -> deny
工具会把文件内容发送给外部模型，例如 morph -> ask
用户明确要求删除某个 workspace 内目录，且路径具体 -> ask
普通只读检索命令 -> allow
```

这一层不是最终语义能力，但它能让接口、测试和策略合并先跑通。

### 8. 实现 shell 静态检查

新增：

```text
gptme/policyguard/shell_static.py
```

复用现有：

```text
gptme/tools/shell_validation.py::is_allowlisted()
gptme/tools/shell_validation.py::is_denylisted()
gptme/tools/shell_validation.py::check_with_shellcheck()
```

第一版 shell 检查策略：

```text
allowlist 命中且语义低风险 -> allow 候选
denylist 命中 -> deny 候选
含删除、覆盖、提权、管道下载执行、工作区外路径 -> ask 或 deny 候选
读取敏感文件 -> ask 或 deny 候选
其他未知命令 -> ask 候选
```

`shellcheck` 是一个 Shell 脚本静态检查工具，用来发现命令语法和常见风险。它不能替代安全策略，只能作为静态检查信号之一。

### 9. 实现 Python / IPython AST 静态检查

新增：

```text
gptme/policyguard/python_static.py
```

使用 Python 标准库 `ast` 解析代码。

重点检查：

```text
subprocess.*
os.system
shutil.rmtree
Path.unlink
Path.rmdir
open(..., "w")
Path.write_text
requests / urllib / socket
os.environ
dotenv
eval
exec
__import__
pickle.load
importlib
```

`AST` 是 Abstract Syntax Tree，中文是抽象语法树。它能把 Python 代码变成结构化节点，便于识别函数调用和属性访问。

第一版要支持常见别名：

```text
import subprocess as sp
sp.run(...)

from pathlib import Path
Path("x").unlink()
```

### 10. 实现文件修改类静态检查

新增：

```text
gptme/policyguard/path_static.py
```

对这些工具启用：

```text
patch
save
append
patch_many
morph
```

重点检查：

```text
路径是否在 workspace 内
是否使用 ../ 跳出工作区
是否使用绝对路径写入 workspace 外
是否修改 .env、credentials、SSH key、密钥文件
是否改动规模过大
是否删除大量内容
是否覆盖二进制文件
是否新增可执行脚本
是否修改 CI/CD、安装脚本或权限配置
```

`morph` 的额外检查：

```text
目标文件是否可能包含凭据或敏感配置
是否会把文件内容发送给外部模型
是否必须要求用户明确确认外部模型调用
```

### 11. 实现决策合并器

在 `evaluator.py` 中实现：

```text
merge_policy_results(semantic_result, static_result) -> PolicyDecision
```

合并规则要保守：

```text
任一层 critical deny -> deny
任一层 high 且不可逆或越权 -> deny
任一层 high 但用户明确授权 -> ask
任一层 medium -> ask，除非明确低风险白名单覆盖
两层均 low 且工具低风险 -> allow
LLM 失败 + 静态不确定 -> ask
ask + 无交互确认能力 -> deny
```

合并器是面试表达里的重点：LLM 负责理解语义，静态检查负责确定性底线，最终由统一决策对象控制执行。

### 12. 接入统一执行入口

在 `ToolUse.execute()` 中、调用具体 `tool.execute(...)` 前插入策略判断。

目标流程：

```text
ToolUse.execute()
  -> evaluate_tool_use(self, workspace, context)
  -> write_policy_event(...)
  -> decision.action == deny:
       yield Message("system", reason)
       return
  -> decision.action == ask:
       get_confirmation(default_confirm=False)
       confirmed: continue
       skipped: yield Message("system", reason); return
  -> tool.execute(...)
```

`default_confirm=False` 的意义是：如果没有可用的确认界面，不要默认执行高风险操作。

需要注意避免双重确认：

```text
PolicyGuard ask 已经确认过的工具，不应马上又被 execute_with_confirmation() 问一次同样的问题。
```

第一版可以接受某些写文件工具仍保留内容 diff 确认，但需要明确区分：

```text
PolicyGuard 确认：是否允许这类高风险操作
工具原有确认：是否确认具体 diff / 编辑内容
```

后续可以通过上下文标记减少重复确认。

### 13. 处理 ask、no_confirm 和 auto-confirm 的关系

`no_confirm` 表示跳过确认。为了安全，`PolicyGuard` 的 `ask` 不能被 `no_confirm` 自动绕过。

第一版规则：

```text
allow -> 继续执行
ask -> 调用 get_confirmation(default_confirm=False)
deny -> 直接阻止
```

如果当前没有交互式确认能力，`ask` 会变成拒绝执行，并返回清晰系统消息。

需要检查：

```text
gptme/hooks/confirm.py::get_confirmation()
gptme/hooks/cli_confirm.py
gptme/hooks/server_confirm.py
gptme/hooks/auto_confirm.py
```

目标不是删除 auto-confirm，而是让 PolicyGuard 的高风险 `ask` 不被自动放行。

### 14. 增加审计日志

在 `gptme/policyguard/audit.py` 中实现追加写入：

```text
<logdir>/policy-events.jsonl
```

记录字段：

```text
timestamp
tool
raw_content
normalized_args
workspace
recent_user_intent
assistant_plan_or_message
semantic_mode
fast_semantic_result
thinking_semantic_result
static_result
final_action
risk_level
reasons
requires_explicit_confirmation
confirmation_result
```

如果 `log` 或 `logdir` 暂时拿不到，第一版可以只跳过文件写入，但不能影响工具执行。

### 15. 第一版限定检查范围

只对这些工具启用强检查：

```text
shell
ipython
python
patch
save
append
patch_many
morph
```

其他工具第一版可以返回 `allow`，但要保留扩展入口，并在审计日志中标记：

```text
policy_scope: not_enforced
```

### 16. 补充测试

建议新增：

```text
tests/test_policyguard.py
tests/test_policyguard_semantic.py
tests/test_policyguard_shell.py
tests/test_policyguard_python.py
tests/test_policyguard_tools.py
```

如果测试结构不宜拆太多文件，可以先合并到一个 `tests/test_policyguard.py`，但测试内容要分组清楚。

测试重点：

```text
语义层：
  Fast allow / suspicious / block
  suspicious 触发 Thinking
  semantic mode off 可稳定运行
  用户意图和工具调用不一致时 deny
  LLM 失败时中高风险不自动放行

shell：
  shell allowlist
  shell denylist
  shell unknown command -> ask
  shell 读取 .env -> ask 或 deny

Python/IPython：
  ipython 普通表达式 -> allow
  ipython subprocess / eval / shutil.rmtree -> ask 或 deny
  AST 能识别别名导入

文件工具：
  patch workspace 内路径
  patch workspace 外路径 -> deny
  save 覆盖敏感文件 -> ask 或 deny
  patch_many 任一 workspace 外路径 -> deny
  morph 普通文件 -> 至少 ask
  morph 敏感文件 -> deny 或强制 ask

集成：
  no_confirm 下 ask 不会自动执行
  policy-events.jsonl 记录关键字段
  普通 chat 无工具调用路径不受影响
```

### 17. 保持已有行为兼容

实现过程中要避免破坏：

```text
shell_validation.py 现有 allowlist / denylist
execute_with_confirmation() 的编辑确认能力
CLI 确认钩子
server 确认钩子
普通只读工具调用
patch_many 原子写入能力
morph 写入前文件未变化校验
```

### 18. 第二阶段：接入真实 Fast / Thinking 语义审查

第一阶段的 `HeuristicSemanticClassifier` 是启发式策略，也就是用人工写好的经验规则判断风险。
它不真正理解完整上下文，只根据命令文本、路径、工具名、静态检查结果等特征做保守判断。

典型启发式规则包括：

```text
命令包含 curl/wget 且管道到 bash/sh/python -> block / deny
命令或路径包含 .env、credentials、id_rsa、secret -> suspicious / ask
工具是 morph -> suspicious / ask，因为会把文件内容发送给外部模型
命令包含 git reset --hard、git clean、rm -rf -> suspicious 或 block
Python AST 中出现 subprocess.run、os.system、shutil.rmtree、eval、exec -> high risk
文件工具目标路径跳出 workspace -> critical / deny
普通只读命令，例如 ls、pwd、git status -> allow
```

启发式策略的优点是稳定、可测试、不需要联网，适合 CI 和安全兜底。
缺点是它主要靠模式匹配和结构特征，无法可靠判断“用户真正想做什么”和“工具调用是否越权”。
第二阶段要补上的就是用真实 LLM-as-Judge 做语义判断。

#### 18.1 第二阶段目标

把当前占位的 Fast / Thinking 分类器升级为可调用真实模型的语义审查器：

```text
HeuristicSemanticClassifier:
  永远保留，作为 off 模式和 LLM 失败时的兜底。

FastSemanticClassifier:
  默认调用轻量、低延迟模型，做快速 allow / suspicious / block 判断。

ThinkingSemanticClassifier:
  默认调用更强模型，对 suspicious、高风险、低置信度或静态检查命中的调用做深度复核。
```

目标调用链：

```text
evaluate_tool_use()
  -> normalize_tool_use()
  -> _run_static_checks()
  -> _run_semantic_checks()
       -> mode=off: HeuristicSemanticClassifier
       -> mode=fast: DeepSeekFastSemanticClassifier
       -> mode=thinking: DeepSeekThinkingSemanticClassifier
       -> mode=both:
            Fast first
            if Fast uncertain/suspicious/block or static risk >= medium:
              Thinking second
  -> merge_policy_results()
```

#### 18.2 模型选择

DeepSeek 官方 API 当前主模型建议使用：

```text
Fast 模型：
  deepseek/deepseek-v4-flash
  用途：低延迟快筛。
  thinking：disabled。
  max_tokens：512 到 1024。
  temperature：0。

Thinking 模型：
  deepseek/deepseek-v4-pro
  用途：深度复核高风险、复杂或不确定工具调用。
  thinking：enabled。
  reasoning_effort：high，必要时 max。
  max_tokens：2048 到 4096。
  temperature/top_p：不要传，thinking 模式下这些参数无效。
```

注意：`deepseek-chat` 和 `deepseek-reasoner` 仍可兼容使用，但 DeepSeek 官方已标注它们会在 `2026-07-24 15:59 UTC` 废弃。
第二阶段不要把旧模型名作为长期默认值。

如果当前 gptme 模型元数据里 direct `deepseek` provider 还没有 `deepseek-v4-flash` 和 `deepseek-v4-pro`，需要补充：

```text
gptme/llm/models/data.py
  deepseek:
    deepseek-v4-flash
    deepseek-v4-pro
```

#### 18.3 配置项

先用环境变量实现，后续可以再映射到 `config.toml`：

```text
GPTME_POLICYGUARD_SEMANTIC_MODE=off|fast|thinking|both
GPTME_POLICYGUARD_FAST_MODEL=deepseek/deepseek-v4-flash
GPTME_POLICYGUARD_THINKING_MODEL=deepseek/deepseek-v4-pro
GPTME_POLICYGUARD_LLM_TIMEOUT=8
GPTME_POLICYGUARD_FAIL_CLOSED=true
GPTME_POLICYGUARD_INCLUDE_ASSISTANT_PLAN=true
```

配置含义：

```text
SEMANTIC_MODE:
  off 使用启发式规则，不调用真实 LLM。
  fast 只调用 Fast 模型。
  thinking 直接调用 Thinking 模型。
  both 先 Fast，必要时 Thinking。

FAST_MODEL:
  快筛模型，默认 deepseek/deepseek-v4-flash。

THINKING_MODEL:
  深度复核模型，默认 deepseek/deepseek-v4-pro。

LLM_TIMEOUT:
  单次语义审查 API 超时时间，避免工具执行链路被长时间阻塞。

FAIL_CLOSED:
  true 表示 LLM 失败时不自动 allow。
  如果静态风险 >= medium，降级为 ask；critical 仍 deny。
```

#### 18.4 结构化 JSON 输出契约

Fast 和 Thinking 都必须输出可解析 JSON，不允许返回普通自然语言作为最终结果。

建议输出结构：

```json
{
  "verdict": "allow | suspicious | block",
  "action_hint": "allow | ask | deny",
  "risk_level": "low | medium | high | critical",
  "confidence": 0.0,
  "requires_thinking": false,
  "reasons": ["short concrete reason"],
  "intent_alignment": "aligned | unclear | misaligned",
  "sensitive_data_risk": false,
  "irreversible_side_effect": false,
  "external_network_or_model_risk": false
}
```

字段解释：

```text
verdict:
  Fast 层主要使用 allow / suspicious / block。

action_hint:
  Thinking 层必须给出更接近最终 PolicyDecision 的 allow / ask / deny 建议。

risk_level:
  必须映射到 RiskLevel。

confidence:
  Fast 低于 0.7 时触发 Thinking。

requires_thinking:
  Fast 显式要求深度复核时触发 Thinking。

intent_alignment:
  判断工具调用是否符合最近用户意图。

sensitive_data_risk:
  是否涉及凭据、密钥、token、SSH key、.env 等敏感内容。

irreversible_side_effect:
  是否可能造成不可逆或难回滚副作用。

external_network_or_model_risk:
  是否会联网下载、上传内容、或把文件发送给外部模型。
```

#### 18.5 Prompt 设计

Fast prompt 要短，目标是快：

```text
System:
  You are PolicyGuard Fast Judge.
  Judge whether a local Agent tool call is safe to execute.
  Return strict JSON only.
  Do not call tools.
  Do not suggest code.

User:
  recent_user_intent
  assistant_plan_or_message
  tool_name
  normalized_args
  static_risk_summary
  workspace
```

Thinking prompt 可以更完整，但必须控制隐私边界：

```text
System:
  You are PolicyGuard Thinking Judge.
  Review whether a local coding Agent tool call should be allowed, require explicit user approval, or be denied.
  Return strict JSON only.
  Do not call tools.
  Do not reveal hidden reasoning.

User:
  recent_user_intent
  assistant_plan_or_message
  tool_name
  normalized_args
  static_check_results
  fast_semantic_result
  workspace
  policy_rules
```

不要把敏感文件内容发送给语义审查模型。
如果工具目标是 `.env`、SSH key、credentials、secret、token 等敏感路径，只发送路径和风险摘要，不发送文件正文。

#### 18.6 LLM 调用实现建议

优先复用 gptme 现有 LLM 路由：

```text
gptme/llm/__init__.py::reply()
  或
gptme/llm/__init__.py::_chat_complete()
```

实现时避免审查模型产生工具调用：

```text
tools=[]
stream=False
output_schema=PolicyGuardSemanticResponse 或 response_format=json_object
```

如果 `output_schema` 对 DeepSeek 兼容性不稳定，可以先用普通 JSON mode：

```text
prompt 中显式包含 json 字样
response_format={"type": "json_object"}
返回后用 json.loads() 解析
解析失败时走失败降级策略
```

为了不影响主聊天模型，PolicyGuard 语义审查不要修改全局默认模型，只在本次调用里显式传入 `FAST_MODEL` 或 `THINKING_MODEL`。

#### 18.7 失败降级策略

语义审查失败不能导致工具执行链路崩溃。

```text
mode=off:
  只使用 HeuristicSemanticClassifier。

mode=fast:
  Fast LLM 成功 -> 使用 Fast 结果。
  Fast LLM 失败 -> 使用 heuristic 结果；如果 static risk >= medium，则 ask。

mode=thinking:
  Thinking LLM 成功 -> 使用 Thinking 结果。
  Thinking LLM 失败 -> 如果 static risk >= medium，则 ask；critical 仍 deny。

mode=both:
  Fast LLM 失败 -> 使用 heuristic 作为 Fast 替代。
  Fast suspicious/block/低置信度 或 static risk >= medium -> 尝试 Thinking。
  Thinking LLM 失败 -> 不自动 allow，至少 ask；critical 仍 deny。
```

所有失败都要记录到审计日志，但不能把 API key、完整敏感文件内容或隐私上下文写入日志。

#### 18.8 审计日志增强

`policy-events.jsonl` 在第二阶段需要区分 Fast 和 Thinking：

```text
semantic_provider
semantic_mode
fast_model
thinking_model
fast_semantic_result
thinking_semantic_result
semantic_error
semantic_latency_ms
semantic_fallback_used
prompt_redaction_applied
```

保留第一阶段已有字段：

```text
tool
raw_content
normalized_args
static_result
final_action
risk_level
reasons
requires_explicit_confirmation
confirmation_result
```

#### 18.9 第二阶段测试

新增或拆分测试：

```text
tests/test_policyguard_semantic_llm.py
tests/test_policyguard_semantic_modes.py
tests/test_policyguard_audit.py
```

测试要求：

```text
不在单元测试中真实调用 DeepSeek API。
通过 monkeypatch/mock 替换 gptme.llm.reply() 或底层调用函数。
```

重点用例：

```text
mode=off 不调用 LLM。
mode=fast 调用 Fast 模型并解析 JSON。
mode=both 中 Fast allow + static low 不触发 Thinking。
mode=both 中 Fast suspicious 触发 Thinking。
mode=both 中 static medium 即使 Fast allow 也触发 Thinking。
Thinking 返回 deny 时最终 PolicyDecision 为 deny。
LLM 返回非法 JSON 时按失败降级策略处理。
LLM 超时时不崩溃，降级为 ask 或 heuristic。
敏感路径不会把文件正文发送给 LLM。
审计日志记录 fast/thinking 模型、结果、失败和 fallback 信息。
```

## 预计改动文件

```text
gptme/tools/base.py
gptme/policyguard/__init__.py
gptme/policyguard/types.py
gptme/policyguard/evaluator.py
gptme/policyguard/semantic.py
gptme/policyguard/shell_static.py
gptme/policyguard/python_static.py
gptme/policyguard/path_static.py
gptme/policyguard/audit.py
gptme/llm/models/data.py
tests/test_policyguard.py
tests/test_policyguard_semantic.py
tests/test_policyguard_semantic_llm.py
tests/test_policyguard_semantic_modes.py
tests/test_policyguard_shell.py
tests/test_policyguard_python.py
tests/test_policyguard_tools.py
tests/test_policyguard_audit.py
```

可能会少量调整：

```text
gptme/tools/shell_validation.py
gptme/hooks/confirm.py
gptme/tools/morph.py
```

如果能通过 `ToolUse.execute()` 统一接入，就尽量少改具体工具文件。

## 风险点

1. 不能让 `PolicyGuard` 和现有确认流程产生混乱，例如同一个工具连续问两次确认。
2. 不能让 `no_confirm` 或 auto-confirm 绕过中高风险 `ask` 决策。
3. 不能把低风险只读命令误拦截太多，否则影响 Agent 可用性。
4. 不能只靠 LLM 判断风险，静态检查必须提供不可绕过的底线。
5. 不能只靠字符串匹配判断 Python 风险，至少要用 `ast` 识别关键调用。
6. 不能让真实 LLM API 成为第一版测试和本地运行的硬依赖。
7. Windows、macOS、Linux 的命令差异很大，第一版规则要保守，避免写死单一系统行为。
8. 审计日志失败不能导致正常工具执行崩溃。
9. `morph` 会把文件内容发送给外部模型，必须把外发风险纳入语义和审计。
10. 第二阶段不能把敏感文件正文、API key、token 或完整凭据内容发送给语义审查模型。
11. Fast / Thinking 模型调用失败时不能自动放行中高风险工具调用。
12. 语义审查必须禁止工具调用，避免 Judge 自己触发新的工具执行。
13. DeepSeek 模型名称会变化，默认模型要使用当前主模型，并保留环境变量覆盖能力。

`macOS` 是苹果电脑的操作系统。`Linux` 是常见服务器和开发环境操作系统。

## 完成标准

1. 高风险工具执行前会经过统一 `PolicyGuard`。
2. `allow / ask / deny` 三类决策能正常影响工具执行。
3. 语义层具备 Fast / Thinking 两阶段接口和运行模式。
4. `off` 模式不依赖真实 LLM，测试可稳定运行。
5. `both` 模式能表达先快筛、必要时深度复核的目标流程。
6. `shell` 复用已有 allowlist / denylist，并增加统一决策输出。
7. `ipython` 和 `python` 使用 `ast` 做结构化检查。
8. 文件修改类工具会检查 workspace 边界和敏感路径。
9. `morph` 会被识别为可能外发代码内容的工具，默认至少需要 ask。
10. `ask` 在无确认能力时不会自动放行。
11. 每次策略判断能写入审计日志。
12. 普通 gptme 对话功能不受影响。
13. 第二阶段 Fast 模型能真实返回结构化语义风险结果。
14. 第二阶段 Thinking 模型能在 suspicious、高风险或低置信度时触发并影响最终决策。
15. LLM 失败、超时或非法 JSON 输出时会按失败降级策略处理，不会静默 allow。
16. 审计日志能记录 fast/thinking 模型、结果、延迟、错误和 fallback 信息。
17. 相关测试通过。
