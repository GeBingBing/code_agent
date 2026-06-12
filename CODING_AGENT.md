# CODING_AGENT.md — Coding Agent Project Instructions

## 技术栈

- Python 3.11+
- OpenAI API（支持 Claude、Ollama、DashScope、Zhipu、MiniMax、Kimi）
- asyncio
- numpy + SQLite（向量记忆）

## 项目结构

```
agent/core/        # 核心引擎（30+ 模块：engine, memory, permissions, plan, spec_loader, event_bus, hooks, dual_review, audit_log, tdd_state_machine, task_state_machine, progress_anchor, embeddings, evolution, subagent_registry, context_builder, user_profile, tool_dispatcher, intent, error_recovery, session, fact_extractor, llm_extractor, hooks_session, text_utils, workspace, config, vector_memory, plan_workflow, ...）
agent/tools/       # 25+ 工具（file_ops, shell, git_tool, git_smart, refactor, grep, sandbox, sub_agent, skill_manager, lsp_tool, test_runner, spec_verifier, code_search, structured_output, plan_mode, todo_tool, notebook_tool, install, memory, audit, diagnostics, glob_tool, web_fetch, web_search, base）
agent/agents/      # 专业化 Agent 角色（orchestrator, evaluator, roles）
agent/hooks/       # 生命周期 hook 处理器（otel, audit, ab_test, dual_review, progress, ralph, task_state）
agent/observability/  # OpenTelemetry（tracing, metrics, logging）
agent/governance/  # AB Testing 框架（ab_test）
agent/lsp/         # LSP 客户端（client）
agent/mcp/         # MCP 协议（client, adapter）
agent/commands/    # Slash 命令系统（base, builtin, user_commands）
agent/llm/         # LLM 客户端（6 providers）
agent/prompts/     # System prompt 组装（assembler）
ui/cli.py          # CLI 入口（原始终端 + 全键盘）
ui/tui.py          # Textual TUI（可选，需 pip install -e .[tui]）
index/             # 代码索引（code_indexer, codmap, retriever）
extensions/vscode/ # VS Code 扩展
docs/              # 22 份 .md（ARCHITECTURE, PR-01..PR-13, SPEC-P0/P1, gap-analysis, 参考, ...）
SPECS.md           # 分阶段规格（含 AC checkbox + 文档状态 banner）
```

## 编码规范

- 遵循 PEP 8
- async/await 显式使用
- 类型注解（dataclass 优先）
- Google 风格 docstring
- 不重复造轮子，优先用成熟库

## 常用命令

```bash
pytest tests/ -q                    # 运行所有测试
pytest tests/ -q -k "not prompt"   # 跳过 prompt 测试
pytest tests/test_<module>.py -q    # 只跑某个模块的测试（推荐用于小幅改动）
python -m agent.core.engine         # 直接运行 engine
```

## 开发流程

1. TDD：先写测试，再写代码
2. 运行 pytest 确保不回归
3. 测试覆盖 1500+ 用例（详见 SPECS.md 文档状态 banner，最新数字 1528 collected / 1519 passed / 1 failed）
