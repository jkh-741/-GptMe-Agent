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

这些机制能处理部分命令白名单、黑名单和用户确认，但整体上还不是一个统一的“工具执行前安全网关”。

## 2. 当前问题

当前安全逻辑分散在不同工具里：

1. `shell` 有自己的 allowlist 和 denylist。
2. `patch`、`save`、`morph` 等写文件工具主要依赖确认流程和路径校验。
3. `ipython` 可以执行 Python 代码，但安全判断不够统一。
4. 是否询问用户、是否自动放行、是否拒绝执行，缺少统一的风险决策对象。
5. 执行前的风险判断和执行后的审计记录不够完整，不方便后续面试讲清楚“为什么这个工具调用被允许或拒绝”。

`allowlist` 是允许列表，表示可以直接放行的规则。`denylist` 是拒绝列表，表示必须阻止的规则。

## 3. 目标

实现一个统一的 `PolicyGuard`，在高风险工具真正执行前进行双重安全筛查：

```text
ToolUse
  -> 参数归一化
  -> 语义风险判断
  -> 结构化静态检查
  -> PolicyDecision(allow / ask / deny)
  -> 用户确认或工具执行
  -> 审计日志
```

`PolicyGuard` 可以理解为“工具执行前的安全门卫”。`PolicyDecision` 是策略决策对象，负责表达最终结果。

`LLM` 是 Large Language Model，中文是“大语言模型”。本任务中的语义风险判断可以先做成可插拔接口，第一版不强依赖真实模型调用；但设计上要允许后续接入 DeepSeek 或其他模型做快速风险判断。

`AST` 是 Abstract Syntax Tree，中文是“抽象语法树”。它把代码解析成结构化节点，比直接查字符串更适合判断 Python 代码里是否调用了删除文件、启动子进程、读取环境变量等危险行为。

## 4. 功能需求

1. 新增统一的 `PolicyGuard` 模块，提供一个主入口函数，例如：

```text
evaluate_tool_use(tool_use, workspace, context) -> PolicyDecision
```

2. `PolicyDecision` 至少包含：

```text
action: allow / ask / deny
risk_level: low / medium / high / critical
reasons: list[str]
checks: list[PolicyCheckResult]
```

`action` 是动作。`risk_level` 是风险等级。`critical` 表示严重风险。

3. 第一版重点覆盖高风险工具：

```text
shell
ipython
patch
save
append
patch_many
```

4. `shell` 检查需要识别：

```text
删除或覆盖大量文件
危险 git 操作
管道下载并执行脚本
提权执行
环境变量和凭据读取
工作区外路径访问
命令组合、管道、重定向
```

5. `ipython` 检查需要使用 Python `ast` 模块识别：

```text
os.system
subprocess.*
shutil.rmtree
Path.unlink / Path.rmdir
open(..., "w") 写文件
socket / requests 网络访问
os.environ / dotenv 凭据读取
eval / exec / __import__ 动态执行
```

6. `patch`、`save`、`append`、`patch_many` 检查需要识别：

```text
工作区外路径
路径穿越，例如 ../
敏感文件，例如 .env、credentials、密钥文件
单次改动过大
删除大量内容
覆盖二进制文件
```

7. `allow` 表示可以继续执行。

8. `ask` 表示必须要求用户显式确认；在非交互或 `no_confirm` 模式下，第一版应按 `deny` 处理，避免自动放行高风险操作。

9. `deny` 表示直接阻止工具执行，并返回清晰的系统消息告诉用户原因。

10. 正常低风险读操作不应被误拦截，例如：

```text
rg
ls
cat
pwd
git status
git diff
```

11. 每次策略决策都应写入审计记录，建议使用 JSON Lines 格式保存到会话目录，例如：

```text
policy-events.jsonl
```

`JSON Lines` 是一行一个 JSON 对象的文本格式，适合追加写日志。

12. 审计记录至少包含：

```text
timestamp
tool
raw_content
normalized_args
workspace
semantic_result
static_result
final_action
risk_level
reasons
```

13. 增加该功能后，不能影响正常的 gptme 对话功能。没有高风险工具调用时，普通启动、用户输入、模型回复、只读工具调用和会话日志写入流程应保持原有行为。

## 5. 非目标

本任务先不实现：

1. 完整 Docker 沙箱。
2. 真实跨平台文件系统隔离。
3. 推测性执行。
4. 分布式权限系统。
5. 对所有第三方 MCP 工具做深度语义审查。
6. 复杂前端权限面板。

`MCP` 是 Model Context Protocol，中文可理解为“模型上下文协议”，用于让模型或 Agent 连接外部工具和数据源。

## 6. 验收标准

1. `ToolUse.execute()` 或同等统一入口会在工具执行前调用 `PolicyGuard`。
2. `shell`、`ipython`、`patch`、`save`、`append`、`patch_many` 会进入统一策略判断。
3. 明显危险的命令或代码会返回 `deny`，不会进入真实执行函数。
4. 中高风险但可由用户决定的操作会返回 `ask`，并且在非交互或 `no_confirm` 模式下不会被自动放行。
5. 低风险只读命令仍可正常执行。
6. Python 代码检查基于 `ast`，不只依赖字符串包含。
7. 文件修改类工具会检查路径是否在 workspace 内。
8. 每次策略判断都会产生审计记录。
9. 现有 `shell_validation.py` 能被复用或迁移，不应简单丢弃已有规则。
10. 相关测试通过，并验证普通 gptme 对话路径不受影响。

## 7. 建议测试

1. `shell` 低风险命令返回 `allow`，例如 `rg "foo" gptme`。
2. `shell` 危险删除命令返回 `deny`，例如 `rm -rf /` 或 Windows 下等价危险命令。
3. `shell` 下载并管道执行脚本返回 `deny` 或 `ask`。
4. `ipython` 中普通表达式返回 `allow`，例如 `2 + 2`。
5. `ipython` 中 `subprocess.run(...)`、`shutil.rmtree(...)` 返回 `ask` 或 `deny`。
6. `ipython` 中 `eval(...)`、`exec(...)` 返回 `ask` 或 `deny`。
7. `patch` 修改 workspace 内普通源码文件可以进入 `ask` 或 `allow`，具体由策略决定。
8. `patch` 修改 workspace 外路径必须 `deny`。
9. `save` 覆盖 `.env` 或凭据文件必须 `ask` 或 `deny`。
10. `no_confirm` 模式下，`ask` 类决策不能被自动执行。
11. 策略审计日志能记录工具名、风险等级、最终动作和原因。
12. 没有工具调用或只有普通对话时，不新增错误、不改变模型回复流程。

## 8. 简历表达方向

实现 PolicyGuard 后，可以表述为：

```text
为本地代码 Agent 设计并实现统一工具执行安全网关，在 shell、Python/IPython、patch/save 等高风险工具执行前进行语义风险判断与 AST/命令结构静态检查，输出 allow/ask/deny 决策并记录可审计日志，降低本地 Agent 自动执行带来的误删、越权和凭据泄露风险。
```

