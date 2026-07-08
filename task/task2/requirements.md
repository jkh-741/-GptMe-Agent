# Task 2: PolicyGuard 双重安全筛查

## 1. 背景

gptme 的 Agent 会从模型回复中解析工具调用，然后执行本地工具，例如 `shell`、`ipython`、`patch`、`save`。

典型调用链：

```text
gptme/chat.py::step()
  -> gptme/tools/__init__.py::execute_msg()
  -> gptme/tools/base.py::ToolUse.iter_from_content()
  -> gptme/tools/base.py::ToolUse.execute()
  -> 具体工具的 execute 函数
```

`Agent` 在这里指“能根据模型输出自动调用工具完成任务的程序”。`shell` 是命令行工具。`IPython` 是 Interactive Python 的缩写，中文可以理解为“交互式 Python 执行环境”。`patch` 是补丁，通常表示对文件内容的结构化修改。

目前项目已经有一些安全机制，例如：

```text
gptme/tools/shell_validation.py
gptme/util/ask_execute.py
gptme/hooks/confirm.py
```

这些机制能处理部分命令白名单、黑名单和用户确认，但整体上还不是一个统一的“工具执行前安全网关”。更重要的是，现有机制主要依赖确定性规则和用户确认，缺少对“用户意图、模型计划、工具调用语义、上下文风险”的统一判断。

本任务要实现的不是单纯补几条静态规则，而是设计一个面向本地代码 Agent 的 `PolicyGuard`：先用两阶段 LLM-as-Judge 做语义风险判断，再用命令解析、Python AST、路径检查等结构化静态规则提供不可绕过的确定性边界。

`LLM-as-Judge` 指使用大语言模型作为评审器，对一次工具调用的意图和风险进行分类。这里的 Judge 不负责执行工具，只负责给出风险判断和理由。

## 2. 当前问题

当前安全逻辑分散在不同工具里：

1. `shell` 有自己的 allowlist 和 denylist。
2. `patch`、`save`、`morph` 等写文件工具主要依赖确认流程和路径校验。
3. `ipython` 可以执行 Python 代码，但安全判断不够统一。
4. 是否询问用户、是否自动放行、是否拒绝执行，缺少统一的风险决策对象。
5. 执行前的风险判断和执行后的审计记录不够完整，不方便后续面试讲清楚“为什么这个工具调用被允许或拒绝”。

更具体地说，当前缺口有两类：

```text
语义层缺口：
  - 不理解用户原始意图和模型计划是否一致。
  - 不区分“用户明确要求删除文件”和“模型自行决定删除文件”。
  - 不识别伪装成普通命令的高风险目标，例如读取凭据、下载脚本并执行。
  - 不支持 fast / thinking 两阶段风险复核。

结构层缺口：
  - shell 检查主要是 allowlist / denylist 和 shellcheck。
  - Python/IPython 没有基于 AST 的危险 API 检查。
  - 文件修改工具没有统一的 workspace、敏感文件、改动规模检查。
  - no_confirm 或 auto-confirm 可能绕过本应显式确认的高风险操作。
```

`allowlist` 是允许列表，表示可以直接放行的规则。`denylist` 是拒绝列表，表示必须阻止的规则。`AST` 是 Abstract Syntax Tree，中文是“抽象语法树”，适合从结构上判断 Python 代码调用了哪些函数和属性。

## 3. 目标

实现一个统一的 `PolicyGuard`，在高风险工具真正执行前进行双重安全筛查：

```text
ToolUse
  -> 参数归一化
  -> Fast semantic classifier
  -> 必要时 Thinking semantic classifier
  -> 结构化静态检查
  -> 合并语义结果和静态结果
  -> PolicyDecision(allow / ask / deny)
  -> 用户确认或工具执行
  -> 审计日志
```

`PolicyGuard` 可以理解为“工具执行前的安全门卫”。`PolicyDecision` 是策略决策对象，负责表达最终结果。

本任务的“安全审查边界”分为两层：

1. 语义层：用 LLM-as-Judge 判断工具调用是否符合用户意图、是否存在越权计划、是否可能造成不可逆副作用。
2. 结构层：用确定性静态检查识别危险命令、危险 Python 调用、工作区外路径、敏感文件和大规模修改。

最终策略不应只依赖 LLM。LLM 负责理解语义和意图，静态规则负责提供可测试、可解释、不可绕过的底线。

## 4. 功能需求

### 4.1 统一入口

新增统一的 `PolicyGuard` 模块，提供一个主入口函数，例如：

```text
evaluate_tool_use(tool_use, workspace, context) -> PolicyDecision
```

第一版建议新增目录：

```text
gptme/policyguard/
  __init__.py
  types.py
  evaluator.py
  semantic.py
  shell_static.py
  python_static.py
  path_static.py
  audit.py
```

`ToolUse.execute()` 或同等统一入口必须在调用具体 `tool.execute(...)` 前执行策略判断。

### 4.2 策略数据结构

`PolicyDecision` 至少包含：

```text
action: allow / ask / deny
risk_level: low / medium / high / critical
reasons: list[str]
checks: list[PolicyCheckResult]
semantic_result: SemanticRiskResult | None
static_result: StaticRiskResult | None
requires_explicit_confirmation: bool
```

`PolicyCheckResult` 至少包含：

```text
name: str
passed: bool
risk_level: low / medium / high / critical
reason: str
evidence: dict
```

`action` 是最终动作。`risk_level` 是风险等级。`critical` 表示严重风险。`requires_explicit_confirmation` 表示是否必须由用户明确确认，不能被 no-confirm 或 auto-confirm 自动放行。

### 4.3 覆盖范围

第一版重点覆盖高风险工具：

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

其中 `morph` 必须特别检查：它会把原文件内容发送给外部 Morph 模型，因此涉及代码和潜在敏感信息外发风险。

其他工具第一版可以默认 `allow`，但要保留扩展入口，并在审计日志中记录“未纳入强检查”。

## 5. LLM-as-Judge 语义检查需求

### 5.1 两阶段分类器

实现两个可插拔语义分类器接口：

```text
class SemanticRiskClassifier:
    def classify(request: SemanticRiskRequest) -> SemanticRiskResult
```

第一阶段是 Fast classifier：

```text
目标：低延迟快速判断
输入：用户最近意图、模型计划、工具名、归一化参数、工作区信息
输出：allow / suspicious / block，以及简短原因
适用：每次高风险工具调用默认执行
```

第二阶段是 Thinking classifier：

```text
目标：深度复核边界案例和高风险案例
输入：更完整的上下文、Fast 结果、静态检查摘要
输出：allow / ask / deny、风险等级、理由
适用：Fast 不确定、Fast 判高风险、静态检查发现中高风险时触发
```

`fast classifier` 是快速分类器，负责快速筛查。`thinking classifier` 是深度推理分类器，负责对疑似危险操作做更细判断。

### 5.2 运行模式

支持三种语义检查模式：

```text
fast: 只运行 Fast classifier
thinking: 直接运行 Thinking classifier
both: 默认模式，先 Fast，必要时 Thinking
```

建议通过配置或环境变量控制，例如：

```text
GPTME_POLICYGUARD_SEMANTIC_MODE=off|fast|thinking|both
```

`off` 表示关闭真实 LLM 语义调用，仅使用本地启发式语义规则和结构化静态检查。第一版必须支持 `off`，方便本地测试和 CI。

### 5.3 第一版降级策略

第一版不强制依赖真实 LLM API。语义层必须设计成可替换接口：

```text
默认实现：
  - 使用轻量规则模拟 semantic result。
  - 不需要联网。
  - 能稳定用于单元测试。

后续实现：
  - 可接入 DeepSeek、OpenAI、OpenRouter 或其他 provider。
  - 可分别配置 fast model 和 thinking model。
```

如果语义分类器调用失败：

```text
低风险只读工具：可以继续依赖静态检查结果。
中高风险工具：不得因为 LLM 失败而自动放行。
明显危险工具：静态检查仍应 deny。
不确定工具：应 ask；无交互确认时 deny。
```

### 5.4 语义判断输入

`SemanticRiskRequest` 至少包含：

```text
tool_name
raw_content
normalized_args
workspace
recent_user_intent
assistant_plan_or_message
conversation_summary 可选
static_findings_so_far 可选
```

其中 `recent_user_intent` 是最近用户明确表达的目标。`assistant_plan_or_message` 是模型产生工具调用时的上下文，用于判断工具调用是否越过用户授权。

### 5.5 语义判断重点

语义层需要判断：

```text
工具调用是否符合用户明确意图
是否执行了用户没有授权的删除、覆盖、提交、推送、联网、凭据读取
是否把敏感代码或密钥发送给外部服务
是否通过复杂命令伪装真实意图
是否存在 prompt injection 诱导工具读取隐私或破坏文件
是否可能产生不可逆副作用
是否应要求用户明确确认
```

示例：

```text
用户要求“查看 git 状态”
模型调用 `git reset --hard`
  -> semantic: deny，理由是工具调用明显偏离用户意图。

用户要求“删除 build 目录”
模型调用 `rm -rf build/`
  -> semantic: ask 或 allow，取决于静态检查和路径边界。

用户要求“总结 .env 里的配置”
模型调用 `cat .env`
  -> semantic: ask 或 deny，理由是读取敏感凭据文件。

morph 修改普通源码文件
  -> semantic: ask，理由是会把文件内容发送给外部模型。
```

### 5.6 语义输出格式

`SemanticRiskResult` 至少包含：

```text
verdict: allow / suspicious / block
risk_level: low / medium / high / critical
confidence: float
reasons: list[str]
requires_thinking: bool
```

Fast classifier 可以输出 `suspicious` 表示无法直接放行，需要 Thinking classifier 或用户确认。

## 6. 结构化静态检查需求

### 6.1 shell 检查

`shell` 检查需要复用或迁移现有：

```text
gptme/tools/shell_validation.py::is_allowlisted()
gptme/tools/shell_validation.py::is_denylisted()
gptme/tools/shell_validation.py::check_with_shellcheck()
```

同时补充识别：

```text
删除或覆盖大量文件
危险 git 操作
管道下载并执行脚本
提权执行
环境变量和凭据读取
工作区外路径访问
命令组合、管道、重定向
curl/wget 下载脚本
chmod/chown 大范围权限修改
进程批量 kill
```

低风险只读命令不应误拦截，例如：

```text
rg
ls
cat 普通源码文件
pwd
git status
git diff
```

### 6.2 Python/IPython AST 检查

`ipython` 和 `python` 检查需要使用 Python `ast` 模块识别：

```text
os.system
subprocess.*
shutil.rmtree
Path.unlink / Path.rmdir
open(..., "w") / Path.write_text 写文件
socket / requests / urllib 网络访问
os.environ / dotenv 凭据读取
eval / exec / __import__ 动态执行
pickle.load / marshal / importlib 动态加载
```

普通表达式、数据分析、打印变量、读取非敏感普通文件可以低风险放行或 ask。

### 6.3 文件修改类工具检查

`patch`、`save`、`append`、`patch_many`、`morph` 检查需要识别：

```text
工作区外路径
路径穿越，例如 ../
绝对路径写入
敏感文件，例如 .env、credentials、密钥文件、SSH 配置
单次改动过大
删除大量内容
覆盖二进制文件
新增可执行脚本
修改 CI/CD、安装脚本、权限配置
```

`morph` 还需要识别：

```text
目标文件是否包含凭据或敏感配置
是否会把工作区私有代码发送给外部模型
是否需要用户明确确认外部模型调用
```

### 6.4 决策合并规则

语义检查和静态检查的合并规则应保守：

```text
任一层 critical deny -> deny
任一层 high 且不可逆或越权 -> deny
任一层 high 但用户明确授权 -> ask
任一层 medium -> ask，除非明确低风险白名单覆盖
两层均 low 且工具低风险 -> allow
LLM 失败 + 静态不确定 -> ask
ask + 无交互确认能力 -> deny
```

`allow` 表示可以继续执行。`ask` 表示必须要求用户显式确认。`deny` 表示直接阻止工具执行，并返回清晰的系统消息告诉用户原因。

## 7. 审计日志需求

每次策略决策都应写入审计记录，建议使用 JSON Lines 格式保存到会话目录：

```text
policy-events.jsonl
```

`JSON Lines` 是一行一个 JSON 对象的文本格式，适合追加写日志。

审计记录至少包含：

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

如果暂时拿不到 `logdir`，第一版可以跳过文件写入，但不能影响工具执行；同时需要在 debug 日志中记录跳过原因。

## 8. 非目标

本任务先不实现：

1. 完整 Docker 沙箱。
2. 真实跨平台文件系统隔离。
3. 推测性执行。
4. 分布式权限系统。
5. 对所有第三方 MCP 工具做深度语义审查。
6. 复杂前端权限面板。
7. 完整 prompt injection 防御系统。
8. 强制依赖真实 LLM API 才能运行。

`MCP` 是 Model Context Protocol，中文可理解为“模型上下文协议”，用于让模型或 Agent 连接外部工具和数据源。

## 9. 验收标准

1. `ToolUse.execute()` 或同等统一入口会在工具执行前调用 `PolicyGuard`。
2. `shell`、`ipython`、`python`、`patch`、`save`、`append`、`patch_many`、`morph` 会进入统一策略判断。
3. 策略判断包含语义层和结构化静态层；第一版即使不开真实 LLM，也必须保留 Fast / Thinking 可插拔接口。
4. 支持 `off`、`fast`、`thinking`、`both` 四种语义检查模式，其中 `both` 是目标默认模式。
5. Fast classifier 能输出 `allow / suspicious / block`，并能触发 Thinking classifier。
6. Thinking classifier 能基于更完整上下文输出最终语义风险判断。
7. 明显危险的命令或代码会返回 `deny`，不会进入真实执行函数。
8. 中高风险但可由用户决定的操作会返回 `ask`，并且在非交互或 `no_confirm` 模式下不会被自动放行。
9. 低风险只读命令仍可正常执行。
10. Python 代码检查基于 `ast`，不只依赖字符串包含。
11. 文件修改类工具会检查路径是否在 workspace 内，并识别敏感文件。
12. `morph` 会被识别为可能外发代码内容的工具，默认至少需要 ask。
13. 每次策略判断都会产生审计记录，记录语义结果、静态结果、最终动作和原因。
14. 现有 `shell_validation.py` 能被复用或迁移，不应简单丢弃已有规则。
15. 增加该功能后，不能影响正常的 gptme 对话功能。没有高风险工具调用时，普通启动、用户输入、模型回复、只读工具调用和会话日志写入流程应保持原有行为。

## 10. 建议测试

### 10.1 语义层测试

1. Fast classifier 对低风险只读命令返回 `allow`。
2. Fast classifier 对明显危险命令返回 `block`。
3. Fast classifier 对边界命令返回 `suspicious`，并触发 Thinking classifier。
4. 用户要求“查看状态”，工具调用 `git reset --hard`，语义结果必须 `deny`。
5. 用户明确要求“删除 build 目录”，工具调用 `rm -rf build/`，语义结果可以 `ask`，但不能直接无条件 `allow`。
6. LLM 语义分类器失败时，中高风险工具不能自动放行。
7. `GPTME_POLICYGUARD_SEMANTIC_MODE=off` 时，测试仍能只依赖本地规则稳定运行。

### 10.2 shell 测试

1. `shell` 低风险命令返回 `allow`，例如 `rg "foo" gptme`。
2. `shell` 危险删除命令返回 `deny`，例如 `rm -rf /` 或 Windows 下等价危险命令。
3. `shell` 下载并管道执行脚本返回 `deny` 或 `ask`。
4. `shell` 读取 `.env` 返回 `ask` 或 `deny`。
5. `shell` 对 `git status`、`git diff` 不应误拦截。

### 10.3 Python/IPython 测试

1. `ipython` 中普通表达式返回 `allow`，例如 `2 + 2`。
2. `ipython` 中 `subprocess.run(...)`、`shutil.rmtree(...)` 返回 `ask` 或 `deny`。
3. `ipython` 中 `eval(...)`、`exec(...)` 返回 `ask` 或 `deny`。
4. `ipython` 中 `os.environ["API_KEY"]` 返回 `ask` 或 `deny`。
5. Python AST 检查能识别别名导入，例如 `import subprocess as sp; sp.run(...)`。

### 10.4 文件工具测试

1. `patch` 修改 workspace 内普通源码文件可以进入 `ask` 或 `allow`，具体由策略决定。
2. `patch` 修改 workspace 外路径必须 `deny`。
3. `save` 覆盖 `.env` 或凭据文件必须 `ask` 或 `deny`。
4. `patch_many` 任一目标文件在 workspace 外时必须 `deny`。
5. `patch_many` 多文件普通源码修改能正常走确认和原子写入。
6. `morph` 修改普通源码文件默认至少 `ask`，因为会调用外部模型。
7. `morph` 目标文件疑似包含密钥时必须 `deny` 或强制 `ask`。

### 10.5 集成和审计测试

1. `no_confirm` 模式下，`ask` 类决策不能被自动执行。
2. 策略审计日志能记录工具名、语义结果、静态结果、风险等级、最终动作和原因。
3. 没有工具调用或只有普通对话时，不新增错误、不改变模型回复流程。
4. 现有 shell allowlist 测试仍通过。
5. 现有 patch/save/patch_many 正常修改测试仍通过。

## 11. 简历表达方向

实现 PolicyGuard 后，可以表述为：

```text
为本地代码 Agent 设计并实现统一工具执行安全网关，在 shell、Python/IPython、patch/save/morph 等高风险工具执行前引入两阶段 LLM-as-Judge 语义审查，并结合 AST、命令结构和路径边界静态检查输出 allow/ask/deny 决策，支持不可绕过的显式确认和 JSONL 审计日志，降低本地 Agent 自动执行带来的误删、越权、凭据泄露和敏感代码外发风险。
```

更偏工程实现的表达：

```text
构建 PolicyGuard 安全运行时：Fast classifier 负责低延迟语义快筛，Thinking classifier 负责高风险深度复核，结构化检查层负责 shell 命令、Python AST 和文件路径的确定性约束，最终通过统一 PolicyDecision 控制工具执行、用户确认和审计记录。
```
