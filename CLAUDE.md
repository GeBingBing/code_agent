# Coding Agent CLAUDE.md

> **本文件角色**：开发者 / AI 助手在 IDE / CLI 中阅读的开发指引。运行时由 agent 自动加载的精简指令见 `CODING_AGENT.md`（注入 system prompt）。两份文件有意分开——本文件可以写得详细，`CODING_AGENT.md` 必须保持精简以节省 context window。

## 项目愿景

构建一个可控、自进化的 AI 编程智能体。参考 smolagents / aider / Claude Code 等开源项目，分阶段迭代交付。

## 技术栈

- Python 3.11+
- OpenAI API（支持 Claude、Ollama、DashScope、Zhipu、MiniMax、Kimi）
- asyncio
- numpy + SQLite（向量记忆）
- httpx（Web 工具）
- prompt_toolkit（可选，CLI 增强）

## 架构原则

- **简单可运行**: 每个特性完成后立即可测试
- **参考而非复制**: 读懂开源项目，用自己的方式实现
- **模块化**: 核心（LLM客户端、工具、主循环）独立拆分
- **不重复造轮子**: 有现成的成熟库直接用

## 项目结构

```
coding-agent/
├── agent/
│   ├── core/                    # 核心引擎（30+ 模块）
│   │   ├── engine.py            # ReAct + plan-then-execute
│   │   ├── event_bus.py         # 事件总线（PR-01）
│   │   ├── hooks.py             # 11 个生命周期 hook 点（PR-01）
│   │   ├── hooks_session.py     # hook session
│   │   ├── plan.py / plan_workflow.py  # ExecutionPlan 数据结构 + 工作流
│   │   ├── memory.py            # 三层记忆
│   │   ├── vector_memory.py     # 向量记忆（SQLite + numpy）
│   │   ├── embeddings.py        # Embedding 抽象（PR-04，Hashing/SBERT/TF-IDF）
│   │   ├── fact_extractor.py / llm_extractor.py  # 记忆事实抽取
│   │   ├── permissions.py       # 权限控制（4 模式）
│   │   ├── dual_review.py       # Dual-agent 互审（PR-11）
│   │   ├── audit_log.py         # 不可变审计日志（PR-08）
│   │   ├── tdd_state_machine.py / tdd_ralph.py  # TDD 强制循环（PR-02）
│   │   ├── task_state_machine.py # 任务状态机（PR-03）
│   │   ├── progress_anchor.py   # progress.txt 进度锚点（PR-13）
│   │   ├── subagent_registry.py # 子 Agent 树管理
│   │   ├── evolution.py         # 自我进化引擎（P2-1）
│   │   ├── spec_loader.py       # SPECS.md 解析 + AC 状态机（PR-06）
│   │   ├── context_builder.py / user_profile.py  # 上下文 + 用户画像
│   │   ├── tool_dispatcher.py   # tool call 分发（并行/串行）
│   │   ├── intent.py            # 意图路由（ask/edit/agent）
│   │   ├── error_recovery.py    # 错误恢复
│   │   ├── session.py           # 会话持久化
│   │   ├── text_utils.py        # 文本工具
│   │   ├── workspace.py         # workspace 路径
│   │   └── config.py            # 配置管理
│   ├── tools/                   # 25+ 工具
│   │   ├── base.py              # 工具基类 + ToolRegistry
│   │   ├── file_ops.py          # 文件操作
│   │   ├── shell.py             # 命令执行
│   │   ├── git_tool.py / git_smart.py  # Git 基础 + 智能操作（P2-3）
│   │   ├── grep.py / glob_tool.py      # 搜索
│   │   ├── code_search.py       # 代码搜索（符号 + 引用）
│   │   ├── refactor.py          # 多文件重构感知（P2-2）
│   │   ├── spec_verifier.py     # specDD 验证
│   │   ├── test_runner.py       # pytest 集成
│   │   ├── sandbox.py           # Docker 沙箱
│   │   ├── lsp_tool.py          # LSP 集成
│   │   ├── skill_manager.py     # 技能管理
│   │   ├── sub_agent.py         # 子 Agent 工具
│   │   ├── memory.py            # 记忆工具
│   │   ├── audit.py             # 审计工具
│   │   ├── diagnostics.py       # 诊断
│   │   ├── install.py           # 依赖安装
│   │   ├── notebook_tool.py     # Jupyter notebook
│   │   ├── plan_mode.py         # plan 模式工具
│   │   ├── structured_output.py # 结构化输出
│   │   ├── todo_tool.py         # todo 列表
│   │   ├── web_fetch.py / web_search.py  # 网络工具
│   ├── agents/                  # 专业化 Agent 角色
│   │   ├── orchestrator.py      # Orchestrator PM（PR-07）
│   │   ├── evaluator.py         # Evaluator（PR-09）
│   │   └── roles.py             # 4 角色定义
│   ├── hooks/                   # 生命周期 hook 处理器
│   │   ├── ab_test.py / audit.py / dual_review.py / otel.py
│   │   ├── progress.py / ralph.py / task_state.py
│   ├── observability/           # OTel 三大支柱（PR-10）
│   │   ├── tracing.py / metrics.py / logging.py
│   ├── governance/              # 治理
│   │   └── ab_test.py           # AB Testing 框架（PR-12）
│   ├── lsp/                     # LSP 客户端
│   │   └── client.py
│   ├── mcp/                     # MCP 协议（P2-4）
│   │   ├── client.py            # JSON-RPC over stdio
│   │   └── adapter.py           # MCPToolAdapter + MCPServerManager
│   ├── commands/                # Slash 命令系统
│   │   ├── base.py              # CommandRegistry
│   │   ├── builtin.py           # 11+ 内置命令
│   │   └── user_commands.py     # 用户 profile 命令
│   ├── llm/
│   │   └── client.py            # LLM 客户端（6 providers）
│   └── prompts/
│       └── assembler.py         # System prompt 组装
├── index/                       # 代码索引
│   ├── code_indexer.py          # Python AST + tree-sitter 跨多语言
│   ├── codmap.py                # Aider repomap 风格（PR-05）
│   └── retriever.py             # 混合检索 + 语义搜索
├── ui/
│   ├── cli.py                   # CLI（原始终端 + 全键盘支持）
│   ├── tui.py                   # Textual TUI（P1-4）
│   └── spinner.py
├── extensions/                  # 编辑器扩展
│   └── vscode/                  # VS Code 扩展（TypeScript）
├── data/                        # 运行时数据
├── skills/                      # 技能库
├── workspace/                   # Agent 工作目录
├── tests/                       # 测试（详见 SPECS.md 文档状态 banner）
├── docs/                        # 文档（22 份 .md）
│   ├── 1.md                     # 长版技术规格说明书
│   ├── ARCHITECTURE.md          # 架构总览
│   ├── CONFIG.md                # 配置参考
│   ├── SPEC-ARCHITECTURE.md     # 架构升级 P1–P6 计划
│   ├── SPEC-P0.md               # P0 实施规格
│   ├── SPEC-P1.md               # P1 实施规格
│   ├── TOOLS.md                 # 工具开发指南
│   ├── gap-analysis.md          # 目标差距分析
│   ├── code-review-issues.md    # 2026-05-22 审查快照（历史）
│   ├── 参考.md                  # 融合蓝图
│   ├── PR-01..PR-13-*.md        # 13 份 PR 设计 + 实现参考
│   └── PR-TEST-REPORT.md        # PR-11/12/13 测试报告
├── CODING_AGENT.md              # 项目指令（Agent 自动加载）
├── CLAUDE.md                    # 本文件
├── README.md                    # 用户 README
├── SPECS.md                     # 分阶段规格（含 AC checkbox + 状态 banner）
├── pyproject.toml               # 包元数据 + 依赖（textual = 可选 [tui] extra）
├── requirements.txt             # 最小运行时依赖
└── main.py                      # 入口
```

## 执行模式

| 模式 | 行为 | 切换方式 |
|------|------|---------|
| `plan` | 只读分析 + 输出计划，不执行 | `/plan` 或 `AGENT_MODE=plan` |
| `default` | 先计划 → 确认 → 执行 | `/mode default` |
| `auto` | 计划 → 自动执行 | `/mode auto` |
| `bypass` | 全自动（CRITICAL 始终拦截） | `/mode bypass` |

## Plan-Then-Execute 架构

```
run(task)
    │
    ├── run_plan(task)     → ExecutionPlan（PLAN 权限 + 只读工具）
    │       └── 用户确认（default mode）/ 自动（auto/bypass）
    │
    └── run_stream(task, plan_context) → ReAct 流式执行
```

## 编码规范

### 基本

- 遵循 PEP 8
- async/await 显式使用
- **不重复造轮子**: 有现成的成熟库直接用
- **保持简单**: 能用现有库解决就不要复杂化

### 命名

- 模块 `snake_case`（如 `audit_log.py`）
- 类 `PascalCase`（如 `AuditLog`）
- 函数 / 变量 `snake_case`（如 `query_by_date`）
- 常量 `UPPER_SNAKE`（如 `MAX_DEPTH = 5`）
- 私有前缀 `_`（如 `_internal_helper`）

### 类型与结构

- **类型注解**：所有公共 API 必加；dataclass 字段可省
- **dataclass 优先**：`@dataclass` 避免 `__init__` boilerplate
- **不要可变默认值**（`def f(x=[])`）；用 `None` + 内部初始化
- **不要函数调用默认值**（`def f(x=datetime.now())`）；用 `None` sentinel

### Docstring（Google 风格）

```python
def foo(x: int) -> int:
    """Short summary.

    Extended description.

    Args:
        x: Description.

    Returns:
        Description.

    Raises:
        ValueError: When x is negative.
    """
```

### 错误处理

- `raise NewError("...") from e` 保留原异常链（不要 `raise NewError(str(e))`）
- 自定义异常放模块顶部、命名 `XxxError`
- 顶层 handler 记录到 `audit_log` 而非 `print`

### Logging

- 用 `from agent.observability.logging import get_logger; logger = get_logger(__name__)`，不要 `logging.getLogger(__name__)`（项目内统一格式）
- 不用 `print` 调试
- 日志级别：debug 详细 / info 关键节点 / warning 异常但可恢复 / error 真错误

### Import 顺序

`stdlib` → `3rd party` → `agent.*` / `ui.*` / `index.*`（isort 在 `pyproject.toml` 已配 `known-first-party`，自动排）

## 质量门禁与开发流程

### 提交前 checklist

1. 阅读 `SPECS.md` 理解目标
2. 参考开源项目的实现（如 `docs/参考.md` / `docs/1.md`）
3. 先写测试，再写代码（TDD）
4. 跑相关测试 + lint（见下）

### Lint / Format

- `ruff check .` — Python lint（CI 也在用，配置在 `pyproject.toml`）
- `black . --check` — format 检查
- `pre-commit run --all-files` — 一次性跑完所有 pre-commit hook（ruff + black）
- 配置：`.pre-commit-config.yaml`

### Pytest 跑法

| 场景 | 命令 |
|------|------|
| 改一个模块 | `pytest tests/test_<module>.py -q` |
| 跑一个测试类 | `pytest tests/test_<module>.py::TestClass -q` |
| 跑单条用例 | `pytest tests/test_<module>.py::TestClass::test_func -q` |
| 跳过 LLM 依赖测试（**推荐**） | `pytest tests/ -q -k "not prompt"` |
| 全量（仅 CI / 跨模块改动时） | `pytest tests/ -q` |

### CI

`.github/workflows/` 自动跑 ruff + black + 全量测试。提交前本地跑一次 `pre-commit run --all-files` 可减少 CI 失败。

## 资源感知开发

**核心原则：能不消耗的资源，就别消耗。**

- **本地开发用 mock LLM**：`python demo.py` 跑模拟场景；或设置 `OPENAI_API_KEY=mock` 让测试走 mock provider
- **跳过 LLM 依赖测试**：`pytest tests/ -q -k "not prompt"`（多数 prompt 测试需要真 API key）
- **targeted test 优先**：默认只跑改动模块的测试，全量留给 CI
- **全量测试耗时**：~1-2 分钟（1528 collected / 1519 passed），CI 跑
- **TUI 是可选**：`pip install -e .[tui]` 才装 textual；不装走 `ui/cli.py`，功能完整
- **sentence-transformers ~90MB**：默认 `EMBEDDING_PROVIDER=auto`，如已装用 SBERT；否则降级为 `hashing`（不下载）。`EMBEDDING_PROVIDER=hashing` 显式跳过
- **Embedding 缓存**：`data/index/documents.pkl` + `data/index/index.faiss` 启动时加载，重复查询零成本
- **VS Code 扩展独立打包**：`extensions/vscode/` 是 TypeScript，`pyproject.toml` 不打包它；`cd extensions/vscode && npm install` 单独装

## 常见坑位

- **SPECS.md 格式稳定**：`agent/core/spec_loader.py:_PHASE_RE` 只识别 `## Phase N`（有空格）和 `## P0-1` / `## P2-3`（连字符子 ID）。**不要写 `Phase-N`**（会断解析）。`### P0-1` 之外的子结构也不动。
- **不要改 SPECS.md banner**（`> 文档状态：...`）：banner 是 spec_loader 看的；改错会再次失败 `test_integration_all_tools`。
- **OpenAI v2.41+ import 行为**：`openai>=2.41` 后 `OPENAI_API_KEY` 即使未设置也不为 `None` 而是占位字符串（如 `"sk-dummy"`）；`tests/conftest.py` 已 `setdefault`，新测试记得同样处理或显式 `monkeypatch.setenv`。
- **`verify_acs(phase_id="P0")` 已知失败**：阶段 ID 应是 `P0-1` / `P0-2` / ...；该问题详见 `docs/PR-TEST-REPORT.md`，短期不动也接受。
- **`demo.py` ≠ engine 实现**：`demo.py` 是 mock 演示，逻辑是 engine 的简化版；不要把它当参考实现（code-review-issues.md #22 已记录）。
- **`extensions/vscode/` 是 TypeScript**：`pyproject.toml` 不打包它，独立 `cd extensions/vscode && npm install`。
- **PR-13 与 PR-03 共用 `progress_anchor.py`**：设计分工是 PR-13 文本格式（`claude-progress.txt`）+ PR-03 结构化 JSON（`task_state.json`），但代码共用文件，改时两边同步。
- **测试 conftest 必装 `OPENAI_API_KEY`**：跑测试前确认环境（conftest 已兜底，但 IDE 跑单条测试不一定加载 conftest）。
- **import 顺序不要手动排**：pre-commit + isort 自动处理；手排反而会和 ruff 冲突。

## 调试与可观测性速查

| 想看什么 | 去哪 |
|---|---|
| 应用日志 | `~/.coding-agent/logs/{date}.log`（`agent/observability/logging.py`） |
| 不可变审计 | `~/.coding-agent/audit/{date}.jsonl`（PR-08，`agent/core/audit_log.py`） |
| OTel spans / metrics | stdout（控制台 exporter 默认开） |
| 任务状态机 | `~/.coding-agent/task_state.json`（结构化） |
| 进度锚点（人类可读） | `WORKSPACE/.claude-progress.txt`（PR-13） |
| 断点续传 | `coding-agent --resume` 读 `task_state.json` 恢复 |
| L3 长期记忆 | `~/.coding-agent/memory.md` |
| 向量索引 | `data/index/documents.pkl` + `data/index/index.faiss` |
| A/B 实验结果 | `agent/governance/ab_test.py` 结果聚合 |
| 单步调试 engine | 入口 `python -m agent.core.engine`；在 `agent/core/engine.py:run_stream` 打断点 |
| 权限拒绝原因 | `agent/core/permissions.py` 抛 `PermissionDeniedError` 时带路径 / 模式 / 原因 |

## PR / Git workflow

### 分支策略

- `main` — 稳定分支，CI 必须绿
- `feature/<short-desc>` — 新功能
- `pr/<NN>-<short-desc>` — 对应 `docs/PR-NN-*.md` 的实现 PR
- `fix/<short-desc>` — bug 修复

### 提交前

```bash
pre-commit run --all-files
pytest tests/test_<changed_module>.py -q
```

### Commit message

- 可用 `git_smart` 工具（`agent/tools/git_smart.py`）基于 diff 自动生成 LLM 风格 message
- 格式：`<scope>: <imperative summary>`（如 `engine: 修复 tool 并行调度死锁`）
- 中文 / 英文均可，但项目内保持一致

### PR 描述

- 复用对应 `docs/PR-NN-*.md` 的"实现参考"作为基线
- 列出改动文件 + 关键决策 + 验证步骤
- 引用 `SPECS.md` 的 AC 编号（如 `Closes AC-P12-1`）

### CI

`.github/workflows/` 自动跑 ruff + black + 全量 `pytest tests/ -q`。CI 红 = 不能 merge。

## 可复用入口（如何添加 X）

| 想加什么 | 怎么做 |
|---|---|
| **新 LLM provider** | 在 `agent/llm/client.py` 注册新类，实现 `chat()` / `stream()` 协议；参考现有 6 个 provider（OpenAI / DashScope / Zhipu / Ollama / Kimi / MiniMax） |
| **新 slash command** | 在 `agent/commands/builtin.py` 加 `@command(name="/foo", description="...")` 装饰器；参考 `/commit` / `/undo` |
| **新 tool** | 在 `agent/tools/<name>.py` 实现 `BaseTool.run()`，用 `registry.register(MyTool())` 注册；详见 `docs/TOOLS.md` |
| **新 PR 设计文档** | 复制 `docs/PR-13-progress-anchor.md` 结构，更新状态为"待实施"；在 `SPECS.md` 对应 Phase 加 AC checkbox；提交时 PR 描述引用 AC 编号 |
| **新 embedding 策略** | 在 `agent/core/embeddings.py` 实现 `EmbeddingProvider` 协议（`encode()` + `dim` 属性），注册到 `get_default_provider()` 工厂 |
| **新 hook** | 在 `agent/hooks/<name>.py` 实现标准 hook（`before_llm_call` / `after_tool_execution` 等 11 个点），在 `AgentEngine` 启动时注册 |
| **新 LLM 测试** | conftest 已 `setdefault` `OPENAI_API_KEY`；需要真 API 的测试加 `@pytest.mark.integration` 标记，CI 才跑 |
| **新子 Agent 角色** | 在 `agent/agents/roles.py` 加 role 定义；在 `agent/agents/orchestrator.py` 注册到调度器 |

## 架构与文档索引

按需查，不要每个都塞进 CLAUDE.md：

| 关心什么 | 看哪份 |
|---|---|
| 引擎架构、模块协作 | `docs/ARCHITECTURE.md` |
| 配置项 / 环境变量 | `docs/CONFIG.md` |
| 工具开发指南（BaseTool / ToolResult） | `docs/TOOLS.md` |
| 13 个 PR 设计 + 实现参考 | `docs/PR-01..PR-13-*.md` |
| 阶段进度 + AC checkbox + 测试数字 | `SPECS.md` 文档状态 banner |
| 目标差距分析 | `docs/gap-analysis.md` |
| 融合蓝图 / 跨项目对标 | `docs/参考.md` |
| 长版技术规格（七层架构 + Harness） | `docs/1.md` |
| 架构升级 P1–P6 计划 | `docs/SPEC-ARCHITECTURE.md` |
| 阶段实施规格 | `docs/SPEC-P0.md` / `docs/SPEC-P1.md` |

## 参考项目

- smolagents: https://github.com/HuggingFace/smolagents
- aider: https://github.com/paul-gauthier/aider
- Memento-Skills: https://github.com/MementoAI/memento-skills
- Claude Code: https://claude.ai/code
