# Agent Instructions for gptme

This file provides agent-specific guidance for working on gptme.
For general project information, see [README.md](README.md) and [docs](https://gptme.org/docs/).

## Git Workflow

- **Default push target**: Commit and push changes directly to `origin/master`
- **Default push scope**: When the user asks to push, include all current local
  modifications and local commits in `master` unless the user explicitly excludes
  something
- **Branches and PRs**: Do not create or maintain separate branches or PRs unless
  the user explicitly requests them
- **Commit format**: Use [Conventional Commits](https://www.conventionalcommits.org/)
  - `feat:` for new features (not just docs)
  - `fix:` for bug fixes
  - `docs:` for documentation only
  - `refactor:`, `test:`, `chore:` as appropriate
- **Stage files explicitly**: Never use `git add .` or `git commit -a`

## Code Style

- **Type hints**: All functions must have type annotations
- **Formatting**: `ruff format` and `ruff check` (run via pre-commit)
- **Type checking**: `mypy` must pass
- **KISS**: Keep it simple - avoid over-engineering
- **Small functions**: Refactor deeply nested code into smaller units
- **Minimal mocking**: Prefer integration tests over heavy mocking

## Testing

Run tests before pushing changes:
```bash
make test           # Fast tests (excludes slow/eval)
make test SLOW=1    # Include slow tests
make typecheck      # mypy
make lint           # ruff + other checks
```

## Project Structure

Key directories:
- `gptme/` - Core library code
  - `gptme/cli/` - CLI entry points
  - `gptme/tools/` - Tool implementations
  - `gptme/llm/` - LLM provider integrations
  - `gptme/server/` - REST API server
- `tests/` - Test suite
- `docs/` - Sphinx documentation (RST + MD)
- `scripts/` - Build and utility scripts

## Core vs gptme-contrib

We aim to keep gptme core small and focused. See [docs/arewetiny.rst](docs/arewetiny.rst).

**Belongs in core (`gptme`):**
- Essential tools (shell, save, patch, browser, vision)
- Core infrastructure (chat loop, message handling, LLM providers)
- Features needed by most users

**Belongs in [gptme-contrib](https://github.com/gptme/gptme-contrib):**
- Specialized tools (Twitter/X, Discord, email)
- Experimental features
- Integrations with specific services
- Multi-agent patterns (consortium)

When in doubt, start in gptme-contrib. If widely adopted, consider upstreaming.

## Performance

We track startup time and code size. See [docs/arewetiny.rst](docs/arewetiny.rst).

CI benchmarks enforce startup thresholds.

## Key Concepts

- **Tool**: A function the assistant can execute (shell, save, patch, etc.)
- **ToolUse**: Parsed representation of a tool invocation in a message
- **Message**: A single message in the conversation
- **LogManager**: Manages conversation history persistence
- **Step**: One LLM generation + tool execution cycle
- **Turn**: Complete user→assistant exchange (may include multiple steps)

See [docs/glossary.md](docs/glossary.md) for full terminology.

## Subsystem Guides

- [webui/AGENTS.md](webui/AGENTS.md) - Web UI architecture and gotchas

## Common Tasks

### Adding a new tool
1. Create `gptme/tools/toolname.py`
2. Implement `ToolSpec` with `execute()` function
3. Tools are auto-discovered - no manual registration needed
4. Add tests in `tests/test_tools_toolname.py`

### Running the server
```bash
uv run gptme-server --port 5000
```

### Working on the web UI
See [webui/AGENTS.md](webui/AGENTS.md) for full setup including dev servers, testing, and architecture notes.

### Building docs
```bash
make docs
```

## User Learning and Resume Goal

The user is studying this project in order to understand its Agent architecture, module design, and implementation details, then build focused improvements that can be packaged as a resume project within about half a month.

When answering the user's later questions about modules, features, or code:
- Use plain, direct Chinese explanations. Prefer intuitive wording over abstract framework language.
- Do not rely on unexplained abbreviations. If a term must be abbreviated, or an abbreviation appears in code/comments, explicitly explain its full name and meaning in Chinese before using it heavily.
- Explicitly describe the function call chain. Name the entry function, the next important functions it calls, and where the key behavior lives.
- Point to concrete files and key functions/classes rather than only giving conceptual summaries.
- Explain what each module is responsible for, how data flows through it, and why that design matters for an Agent system.
- Prioritize the Agent-related core: CLI conversation loop, tool discovery/execution, message/tool-use parsing, context construction, LLM provider calls, logs/checkpoints, and safety boundaries.
- Keep suggestions shaped by the user's job-search timeline. Favor changes that are interviewable, feasible in days, and easy to explain over broad rewrites.
- When a module or implementation principle is important enough to become study material, the user may ask to save a summary to Notion. Only write to Notion when the user explicitly asks, or after asking for and receiving the user's approval.
- The target Notion location for these notes is `主页\准备的项目\GptMe`. Use the configured Notion MCP/plugin when writing approved notes.
- Maintain the Notion interview question bank as a separate resume-preparation section. Its overall organization should use the "Question and Answer" form: write each likely interviewer question, then write a concise but strong answer that shows real implementation experience, concrete tradeoffs, and project-specific details. Do not turn it into broad theory notes. Organize questions under four dimensions: gptme project and Agent loop fundamentals, Prompt Queue reliability, PolicyGuard safety review, and speculative execution.

The user's current framing for a resume-oriented direction is:

> Design and implement a safe local code-execution Agent runtime on top of gptme's tool-calling framework, combining LLM semantic review, AST/static checks, checkpointing/sandboxing, and speculative execution.

High-value implementation themes:
- Unified `PolicyGuard` before risky tools such as shell, python, and patch.
- Two-layer permission checks: LLM intent/risk review plus structured checks with command parsing, Python AST, or Tree-sitter.
- Speculative execution using isolated directories or git worktrees to try candidate patches/commands, run tests/lint, then merge the best result.
- Auditable execution logs that record user intent, model plan, parsed command structure, risk level, approval result, output, and rollback point.
- Sandbox and rollback design using temporary directories, git checkpoints, file-scope limits, network limits, dangerous-command blocking, and environment-variable controls.

For resume packaging, avoid framing the work as merely "modifying gptme". Frame it as building a local coding Agent safety runtime and speculative execution framework, with gptme as the foundation and research base.

## Local Study Notes

The local checkout is at `D:\Desktop\gptme`, with the virtual environment at `D:\Desktop\gptme\.venv`.

Previously verified local state:
- Version: `gptme v0.31.1.dev202604277+7061f0bbc`
- Python: `3.11.15` on Windows
- Install mode: editable pip install
- Tools: 26 available
- DeepSeek credentials are configured locally through gptme's user credential store.

Useful local startup commands:
```powershell
Set-Location D:\Desktop\gptme
conda activate D:\Desktop\gptme\.venv
$env:PYTHONUTF8="1"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\gptme.exe
```

DeepSeek example:
```powershell
$env:PYTHONUTF8="1"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\gptme.exe -m deepseek/deepseek-chat
```

PolicyGuard semantic review startup:
```powershell
$env:GPTME_POLICYGUARD_SEMANTIC_MODE="both"
$env:LLM_API_TIMEOUT="15"
.\.venv\Scripts\gptme.exe -m deepseek/deepseek-chat
```

`GPTME_POLICYGUARD_SEMANTIC_MODE` controls whether PolicyGuard calls the semantic
review model before risky tool execution:
- `off`: disable model semantic review; use local heuristic rules plus structured static checks.
- `fast`: run only Fast semantic review for low-latency triage.
- `thinking`: run only Thinking semantic review for deeper but slower review.
- `both`: run Fast first, then run Thinking when Fast is suspicious, low-confidence, requires thinking, or static risk is medium or higher.

Current default semantic review models:
- Fast: `deepseek/deepseek-chat`
- Thinking: `deepseek/deepseek-reasoner`

Override the semantic review models with:
```powershell
$env:GPTME_POLICYGUARD_FAST_MODEL="deepseek/deepseek-chat"
$env:GPTME_POLICYGUARD_THINKING_MODEL="deepseek/deepseek-reasoner"
```

`LLM_API_TIMEOUT` is gptme's existing global model API timeout in seconds.
PolicyGuard semantic review reuses the gptme LLM call path, so it is affected by
the same timeout. If semantic review times out, fails on network/API errors,
returns empty content, or returns invalid JSON, PolicyGuard falls back to local
heuristic review and records the failure in `SemanticRiskResult.error`.

Important files and directories for the user's learning path:
- `gptme/cli/main.py` - CLI entry point, conversation flow, tool selection, architect/editor modes.
- `gptme/tools/` - Tool system, including shell, python, read, save, patch, browser, MCP, and related tools.
- `gptme/tools/base.py` - Tool abstractions.
- `gptme/tools/shell.py` - Shell execution behavior.
- `gptme/tools/shell_validation.py` - Shell command validation and safety checks.
- `gptme/llm/` - Provider adapters such as OpenAI, Anthropic, mock providers, and others.
- `gptme/logmanager/` - Conversation logs, checkpoints, and event logs.
- `gptme/context/` - Project context construction.
- `gptme/prompts/` - System prompts, architect prompts, and related prompt assets.
- `gptme/server/` - REST API and Web UI related code.
- `gptme/mcp/` - MCP integration.
