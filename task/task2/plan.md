# Task 2 实现计划：PolicyGuard 双重安全筛查

## 目标

在 gptme 的工具执行链路中加入统一 `PolicyGuard`，让高风险工具在真正执行前先经过策略判断。

目标调用链：

```text
gptme/chat.py::step()
  -> gptme/tools/__init__.py::execute_msg()
  -> gptme/tools/base.py::ToolUse.execute()
  -> gptme/policyguard/evaluator.py::evaluate_tool_use()
  -> allow / ask / deny
  -> 具体工具 execute 函数
```

`PolicyGuard` 是工具执行前的安全门卫。`allow` 是允许执行，`ask` 是要求用户确认，`deny` 是拒绝执行。

## 实现步骤

1. 梳理现有工具执行入口

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
   ```

   需要确认模型生成的工具调用如何从消息文本变成 `ToolUse`，再如何进入具体工具。

2. 新增策略数据结构

   建议新增目录：

   ```text
   gptme/policyguard/
   ```

   先实现：

   ```text
   gptme/policyguard/types.py
   gptme/policyguard/evaluator.py
   gptme/policyguard/audit.py
   ```

   核心类型：

   ```text
   PolicyAction: allow / ask / deny
   RiskLevel: low / medium / high / critical
   PolicyCheckResult
   PolicyDecision
   ```

   `PolicyAction` 是策略动作。`RiskLevel` 是风险等级。

3. 接入统一执行入口

   在 `ToolUse.execute()` 中、调用具体 `tool.execute(...)` 前插入策略判断。

   目标流程：

   ```text
   ToolUse.execute()
     -> evaluate_tool_use(self, workspace, log)
     -> write_policy_event(...)
     -> decision.action == deny: yield Message("system", reason); return
     -> decision.action == ask: get_confirmation(default_confirm=False)
     -> confirmed: continue
     -> skipped: yield Message("system", reason); return
     -> tool.execute(...)
   ```

   `default_confirm=False` 的意义是：如果没有可用的确认界面，不要默认执行高风险操作。

4. 第一版限定检查范围

   只对这些工具启用强检查：

   ```text
   shell
   ipython
   patch
   save
   append
   patch_many
   ```

   其他工具第一版可以返回 `allow`，但要保留扩展入口。

5. 复用现有 shell 安全规则

   阅读并复用：

   ```text
   gptme/tools/shell_validation.py::is_allowlisted()
   gptme/tools/shell_validation.py::is_denylisted()
   gptme/tools/shell_validation.py::check_with_shellcheck()
   ```

   第一版 shell 检查策略：

   ```text
   allowlist 命中 -> allow
   denylist 命中 -> deny
   含删除、覆盖、提权、管道下载执行、工作区外路径 -> ask 或 deny
   其他未知命令 -> ask
   ```

   `shellcheck` 是一个 Shell 脚本静态检查工具，用来发现命令语法和常见风险。

6. 实现 Python / IPython 静态检查

   新增：

   ```text
   gptme/policyguard/python_static.py
   ```

   使用 Python 标准库 `ast` 解析代码。

   重点检查：

   ```text
   subprocess
   os.system
   shutil.rmtree
   Path.unlink
   Path.rmdir
   open(..., "w")
   requests
   socket
   os.environ
   eval
   exec
   __import__
   ```

   `AST` 是 Abstract Syntax Tree，中文是抽象语法树。它能把 Python 代码变成结构化节点，便于识别函数调用和属性访问。

7. 实现文件修改类工具检查

   新增：

   ```text
   gptme/policyguard/path_static.py
   ```

   对 `patch`、`save`、`append`、`patch_many` 重点检查：

   ```text
   路径是否在 workspace 内
   是否使用 ../ 跳出工作区
   是否修改 .env、credentials、密钥文件
   是否改动规模过大
   是否可能覆盖二进制文件
   ```

   `workspace` 是工具允许操作的项目工作目录。

8. 预留语义判断接口

   新增：

   ```text
   gptme/policyguard/semantic.py
   ```

   第一版可以实现为可替换接口：

   ```text
   classify_semantic_risk(tool_use, context) -> PolicyCheckResult
   ```

   默认实现先使用轻量规则，不强制调用真实大语言模型。后续再接入 DeepSeek 或其他模型时，只替换这一层。

   设计时保留两阶段模式：

   ```text
   fast: 快速判断
   thinking: 深度复核
   both: 先快速判断，必要时深度复核
   ```

   `fast classifier` 是快速分类器。`thinking classifier` 是深度推理分类器。

9. 增加审计日志

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
   semantic_result
   static_result
   final_action
   risk_level
   reasons
   ```

   如果 `log` 或 `logdir` 暂时拿不到，第一版可以只跳过文件写入，但不能影响工具执行。

10. 处理 ask 和 no_confirm 的关系

   `no_confirm` 表示跳过确认。为了安全，`PolicyGuard` 的 `ask` 不能被 `no_confirm` 自动绕过。

   第一版规则：

   ```text
   allow -> 继续执行
   ask -> 调用 get_confirmation(default_confirm=False)
   deny -> 直接阻止
   ```

   如果当前没有交互式确认能力，`ask` 会变成跳过执行。

11. 补充测试

   建议新增：

   ```text
   tests/test_policyguard.py
   tests/test_policyguard_shell.py
   tests/test_policyguard_python.py
   tests/test_policyguard_tools.py
   ```

   如果测试结构不宜拆太多文件，可以先合并到一个 `tests/test_policyguard.py`。

   测试重点：

   ```text
   shell allowlist
   shell denylist
   shell unknown command -> ask
   ipython 普通表达式 -> allow
   ipython subprocess / eval / shutil.rmtree -> ask 或 deny
   patch workspace 内路径
   patch workspace 外路径 -> deny
   no_confirm 下 ask 不会自动执行
   policy-events.jsonl 记录关键字段
   普通 chat 无工具调用路径不受影响
   ```

12. 保持已有行为兼容

   实现过程中要避免破坏：

   ```text
   shell_validation.py 现有 allowlist / denylist
   execute_with_confirmation() 的编辑确认能力
   CLI 确认钩子
   server 确认钩子
   普通只读工具调用
   ```

   `CLI` 是 Command Line Interface，中文是命令行接口。

## 预计改动文件

```text
gptme/tools/base.py
gptme/policyguard/__init__.py
gptme/policyguard/types.py
gptme/policyguard/evaluator.py
gptme/policyguard/shell_static.py
gptme/policyguard/python_static.py
gptme/policyguard/path_static.py
gptme/policyguard/semantic.py
gptme/policyguard/audit.py
tests/test_policyguard.py
```

可能会少量调整：

```text
gptme/tools/shell_validation.py
gptme/hooks/confirm.py
```

如果能通过 `ToolUse.execute()` 统一接入，就尽量少改具体工具文件。

## 风险点

1. 不能让 `PolicyGuard` 和现有确认流程产生混乱，例如同一个工具连续问两次确认。
2. 不能让 `no_confirm` 绕过中高风险 `ask` 决策。
3. 不能把低风险只读命令误拦截太多，否则影响 Agent 可用性。
4. 不能只靠字符串匹配判断 Python 风险，至少要用 `ast` 识别关键调用。
5. Windows、macOS、Linux 的命令差异很大，第一版规则要保守，避免写死单一系统行为。
6. 审计日志失败不能导致正常工具执行崩溃。

`macOS` 是苹果电脑的操作系统。`Linux` 是常见服务器和开发环境操作系统。

## 完成标准

1. 高风险工具执行前会经过统一 `PolicyGuard`。
2. `allow / ask / deny` 三类决策能正常影响工具执行。
3. `shell` 复用已有 allowlist / denylist，并增加统一决策输出。
4. `ipython` 使用 `ast` 做结构化检查。
5. 文件修改类工具会检查 workspace 边界和敏感路径。
6. `ask` 在无确认能力时不会自动放行。
7. 每次策略判断能写入审计日志。
8. 普通 gptme 对话功能不受影响。
9. 相关测试通过。

