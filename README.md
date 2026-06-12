# Coding Agent

类 Claude Code 的 AI 编程智能体 — ReAct 循环 + 工具调用 + 记忆 + 权限 + plan-then-execute。

## 功能特性

- **多 Provider LLM**：OpenAI、DashScope（阿里云）、Zhipu（智谱）、MiniMax、Kimi（Moonshot）、Ollama（本地）
- **Plan-then-Execute**：先分析需求生成结构化执行计划，确认后再逐步执行
- **ReAct 主循环**：流式输出，实时展示 tool call 和结果
- **Slash 命令系统**：`/clear` `/plan` `/commit` `/help` `/model` `/mode` `/memory` `/status` `/context` `/review` `/undo` `/orchestrate` `/audit` `/dual-review` `/ab` `/progress` `/evaluate`（11+ 命令）
- **三层记忆**：L1 工作记忆（自动裁剪）、L2 会话摘要、L3 长期记忆（持久化 + 向量语义搜索，Hashing / SBERT / TF-IDF 三种 embedding 抽象）
- **20+ 内置工具**：文件读写/diff、命令执行、git、grep、代码搜索、技能管理、子 Agent、Docker 沙箱、快照/回滚、run_tests、refactor、LSP、structured output、todo、notebook
- **四级权限**：plan（只读）/ default（确认）/ auto（自动）/ bypass（全自动，CRITICAL 操作始终拦截）
- **MCP 工具扩展**（P2-4）：MCPToolAdapter + MCPServerManager（JSON-RPC over stdio）
- **Git 深度集成**（P2-3）：基于 diff 的 LLM 智能 commit message、gh CLI PR 创建、根据任务自动生成分支名
- **自我进化**（P2-1）：EvolutionEngine — 成功任务自动转化为 skills，失败模式学习
- **多文件重构感知**（P2-2）：跨文件引用分析 + safe_rename + dry_run 预览
- **Textual TUI**（P1-4）：可选 `pip install -e .[tui]` 启用，提供面板/进度条/语法高亮
- **EventBus + Hook 系统**（PR-01）：11 个生命周期 hook 点 + A/B Testing / Dual-agent 互审 / 不可变审计日志 / OpenTelemetry
- **Aider repomap 注入**（PR-05）：项目代码地图自动注入 system prompt
- **Dual-agent 互审**（PR-11）：CRITICAL 操作由第二个独立 Agent 复审
- **Orchestrator PM Agent**（PR-07）：4 角色 DAG 调度 — CodeGenerator / TestEngineer / Reviewer / DevOps
- **Evaluator Agent**（PR-09）：完成度 / 代码质量 / 安全性 / 性能 4 维评分 + `SCORE.md`
- **CODING_AGENT.md**：项目级指令文件，Agent 启动时自动加载注入 system prompt
- **TDD 引导 + 强制**（P0-3 + PR-02）：引导式 + 强制 Red→Green→Refactor 状态机 + Ralph 监督
- **任务状态机 + 断点续传**（PR-03 + PR-13）：INIT→PLAN→EXEC→TEST→REVIEW→DONE + `--resume` + `claude-progress.txt`
- **子 Agent 树**：spawn / kill / 生命周期管理，最大深度 5 层
- **终端体验**：方向键光标移动、上下键历史导航、Ctrl+A/E/B/F/W/U/K 快捷键、CJK 双宽度字符支持

## 快速开始

```bash
# 安装
pip install -e .

# 配置 API Key
cp .env.example .env
# 编辑 .env 填入 DASHSCOPE_API_KEY 或 OPENAI_API_KEY

# 交互模式
coding-agent

# 单次任务
coding-agent "用 Python 写一个斐波那契函数"

# 本地模型
DEFAULT_PROVIDER=ollama DEFAULT_MODEL=llama3.2 coding-agent "写一个 hello.py"
```

## 项目指令文件

在 workspace 根目录创建 `CODING_AGENT.md`，Agent 启动时自动加载。示例：

```markdown
## 技术栈
- Python 3.11+, FastAPI, pytest

## 编码规范
- 遵循 PEP 8，类型注解用 dataclass

## 常用命令
pytest tests/ -q
```

用 `/status` 查看是否成功加载。

## 权限模式

| 模式 | 行为 |
|------|------|
| `plan` | 只读分析 + 输出计划，不执行 |
| `default` | 高风险操作需确认 |
| `auto` | 自动判断，低风险直接执行 |
| `bypass` | 全自动（CRITICAL 始终拦截） |

切换：`/mode <name>` 或 `AGENT_MODE=mode coding-agent`

## 项目结构

```
coding-agent/
├── agent/
│   ├── core/                # 核心引擎（30+ 模块）
│   │   ├── engine.py        # ReAct + plan-then-execute
│   │   ├── event_bus.py     # 事件总线（PR-01）
│   │   ├── hooks.py         # 11 个生命周期 hook 点
│   │   ├── plan.py / plan_workflow.py
│   │   ├── memory.py / vector_memory.py / embeddings.py
│   │   ├── permissions.py / dual_review.py / audit_log.py
│   │   ├── tdd_state_machine.py / tdd_ralph.py / task_state_machine.py
│   │   ├── progress_anchor.py
│   │   ├── subagent_registry.py / evolution.py / spec_loader.py
│   │   ├── context_builder.py / user_profile.py / tool_dispatcher.py
│   │   ├── intent.py / error_recovery.py / session.py
│   │   └── config.py
│   ├── tools/               # 25+ 工具
│   │   ├── file_ops.py / shell.py / git_tool.py / git_smart.py
│   │   ├── grep.py / glob_tool.py / code_search.py
│   │   ├── refactor.py / spec_verifier.py / test_runner.py
│   │   ├── sandbox.py / lsp_tool.py / skill_manager.py
│   │   ├── sub_agent.py / memory.py / audit.py
│   │   ├── diagnostics.py / install.py / notebook_tool.py
│   │   ├── plan_mode.py / structured_output.py / todo_tool.py
│   │   ├── web_fetch.py / web_search.py
│   │   └── base.py          # 工具基类 + ToolRegistry
│   ├── agents/              # 专业化 Agent 角色
│   │   ├── orchestrator.py  # Orchestrator PM（PR-07）
│   │   ├── evaluator.py     # Evaluator（PR-09）
│   │   └── roles.py
│   ├── hooks/               # lifecycle hook handlers
│   ├── observability/       # OTel（PR-10）：tracing / metrics / logging
│   ├── governance/          # AB Testing（PR-12）
│   ├── lsp/                 # LSP 客户端
│   ├── mcp/                 # MCP 协议（P2-4）
│   ├── commands/            # Slash 命令（11+）
│   ├── llm/                 # LLM 客户端
│   └── prompts/             # System prompt 组装
├── index/                   # 代码索引（AST + tree-sitter + codmap）
├── ui/                      # CLI + Textual TUI
├── extensions/vscode/       # VS Code 扩展
├── data/                    # 运行时数据
├── skills/                  # 技能库
├── workspace/               # Agent 工作目录
├── tests/                   # 测试（详见 SPECS.md 文档状态 banner）
├── docs/                    # 22 份 .md（ARCHITECTURE、PR-01..13、SPEC-P0/P1 等）
├── demo.py                  # Mock LLM 演示
├── CODING_AGENT.md          # 本项目指令文件
├── CLAUDE.md                # 开发指引
├── README.md                # 本文件
├── SPECS.md                 # 分阶段规格（AC checkbox + 状态 banner）
├── pyproject.toml           # 包元数据 + 依赖（textual = 可选 [tui] extra）
└── requirements.txt
```

## 开发

```bash
# 安装（含 TUI）
pip install -e .[tui]

# 运行所有测试
pytest tests/ -q

# 跳过 prompt 测试
pytest tests/ -q -k "not prompt"

# Mock 模式演示
python demo.py
```
