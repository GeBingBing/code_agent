# Coding Agent CLAUDE.md

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
│   ├── core/
│   │   ├── engine.py            # ReAct + plan-then-execute
│   │   ├── plan.py              # ExecutionPlan 数据结构
│   │   ├── memory.py            # 三层记忆
│   │   ├── permissions.py       # 权限控制
│   │   ├── vector_memory.py     # 向量记忆（SQLite + numpy）
│   │   ├── subagent_registry.py # 子 Agent 树管理
│   │   ├── plugin_hooks.py      # 插件钩子
│   │   ├── evolution.py         # 自我进化引擎（P2-1）
│   │   ├── spec_loader.py       # SPECS.md 加载器
│   │   └── config.py            # 配置管理
│   ├── tools/
│   │   ├── file_ops.py          # 文件操作
│   │   ├── shell.py             # 命令执行
│   │   ├── git_tool.py          # Git 基础操作
│   │   ├── git_smart.py         # Git 智能操作（P2-3）
│   │   ├── grep.py              # 全文搜索
│   │   ├── skill_manager.py     # 技能管理
│   │   ├── sub_agent.py         # 子 Agent 工具
│   │   ├── sandbox.py           # Docker 沙箱
│   │   ├── code_search.py       # 代码搜索
│   │   ├── web_fetch.py         # URL 抓取
│   │   ├── web_search.py        # 网络搜索
│   │   ├── test_runner.py       # pytest 集成
│   │   ├── refactor.py          # 多文件重构感知（P2-2）
│   │   ├── spec_verifier.py     # specDD 验证
│   │   └── base.py              # 工具基类 + ToolRegistry
│   ├── commands/
│   │   ├── base.py              # CommandRegistry
│   │   └── builtin.py           # 内置 /commands
│   ├── llm/
│   │   └── client.py            # LLM 客户端（6 providers）
│   ├── mcp/
│   │   ├── client.py            # MCP JSON-RPC 客户端（P2-4）
│   │   └── adapter.py           # MCP 工具适配 + 服务器管理（P2-4）
│   └── prompts/
│       └── assembler.py         # System prompt 组装
├── index/
│   ├── code_indexer.py          # Python AST + 多语言 regex + tree-sitter
│   └── retriever.py             # 混合检索 + 语义搜索
├── ui/
│   ├── cli.py                   # CLI（原始终端 + 全键盘支持）
│   └── tui.py                   # Textual TUI
├── skills/                      # 技能库
├── workspace/                   # Agent 工作目录
├── tests/                       # 测试（271 全部通过）
├── docs/                        # 文档
│   ├── gap-analysis.md          # 目标差距分析
│   ├── SPEC-P0.md              # P0 实施规格
│   ├── SPEC-P1.md              # P1 实施规格
│   └── code-review-issues.md    # 代码审查报告
├── CODING_AGENT.md              # 项目指令（Agent 自动加载）
├── SPECS.md                     # 分阶段规格
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

- 遵循 PEP 8
- async/await 显式使用
- 类型注解（dataclass 优先）
- Google 风格 docstring
- **不重复造轮子**: 有现成的成熟库直接用
- **保持简单**: 能用现有库解决就不要复杂化

## 开发流程

1. 阅读 SPECS.md 理解目标
2. 参考开源项目的实现
3. 先写测试，再写代码
4. 只运行变动相关测试，不要每次都跑全量：
   - `pytest tests/test_<module>.py -q` — 只跑有变动的模块
   - `pytest tests/test_<module>.py::TestClass::test_func -q` — 只跑单条
   - 仅当改动影响核心引擎（engine/memory/permissions/plan）时才跑 `pytest tests/ -q`

## 参考项目

- smolagents: https://github.com/HuggingFace/smolagents
- aider: https://github.com/paul-gauthier/aider
- Memento-Skills: https://github.com/MementoAI/memento-skills
- Claude Code: https://claude.ai/code
