# CODING_AGENT.md — Coding Agent Project Instructions

## 技术栈

- Python 3.11+
- OpenAI API（支持 Claude、Ollama、DashScope、Zhipu、MiniMax、Kimi）
- asyncio
- numpy + SQLite（向量记忆）

## 项目结构

```
agent/core/        # 核心引擎 (engine, memory, permissions, evolution, plan, spec_loader)
agent/tools/       # 工具 (file_ops, shell, git_tool, git_smart, refactor, grep, sandbox, etc.)
agent/commands/    # Slash 命令系统
agent/llm/         # LLM 客户端（6 providers）
agent/mcp/         # MCP 集成 (client, adapter)
agent/prompts/     # System prompt 组装
ui/cli.py          # CLI 入口（原始终端 + 全键盘）
ui/tui.py          # Textual TUI
index/             # 代码索引 (code_indexer, retriever)
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
python -m agent.core.engine         # 直接运行 engine
```

## 开发流程

1. TDD：先写测试，再写代码
2. 运行 pytest 确保不回归
3. 测试覆盖 271 用例
