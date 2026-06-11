# Coding Agent

类 Claude Code 的 AI 编程智能体 — ReAct 循环 + 工具调用 + 记忆 + 权限 + plan-then-execute。

## 功能特性

- **多 Provider LLM**：OpenAI、DashScope（阿里云）、Zhipu（智谱）、MiniMax、Kimi（Moonshot）、Ollama（本地）
- **Plan-then-Execute**：先分析需求生成结构化执行计划，确认后再逐步执行
- **ReAct 主循环**：流式输出，实时展示 tool call 和结果
- **Slash 命令系统**：`/clear` `/plan` `/commit` `/help` `/model` `/mode` `/memory` `/status` `/context` `/review` `/undo`
- **三层记忆**：L1 工作记忆（自动裁剪）、L2 会话摘要、L3 长期记忆（持久化 + 向量语义搜索）
- **20+ 内置工具**：文件读写/diff、命令执行、git、grep、代码搜索、技能管理、子 Agent、Docker 沙箱、快照/回滚、run_tests
- **四级权限**：plan（只读）/ default（确认）/ auto（自动）/ bypass（全自动，CRITICAL 操作始终拦截）
- **CODING_AGENT.md**：项目级指令文件，Agent 启动时自动加载注入 system prompt
- **TDD 引导**：run_tests 工具 + system prompt 建议先写测试
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
│   ├── core/
│   │   ├── engine.py        # ReAct + plan-then-execute
│   │   ├── plan.py          # ExecutionPlan / PlanStep
│   │   ├── memory.py        # 三层记忆
│   │   ├── permissions.py   # 权限控制
│   │   ├── vector_memory.py # 向量记忆
│   │   ├── plugin_hooks.py  # 插件钩子
│   │   └── subagent_registry.py
│   ├── tools/
│   │   ├── file_ops.py      # 文件读写/diff
│   │   ├── shell.py         # 命令执行
│   │   ├── git_tool.py      # Git 操作
│   │   ├── grep.py          # 全文搜索
│   │   ├── skill_manager.py # 技能管理
│   │   ├── sub_agent.py     # 子 Agent
│   │   ├── sandbox.py       # Docker 沙箱
│   │   ├── code_search.py   # 代码搜索
│   │   ├── web_fetch.py     # URL 抓取
│   │   ├── web_search.py    # 网络搜索
│   │   ├── test_runner.py   # pytest 集成
│   │   └── base.py          # 工具基类 + 注册表
│   ├── commands/            # Slash 命令系统
│   │   ├── base.py          # CommandRegistry
│   │   └── builtin.py       # 11 个内置命令
│   ├── llm/
│   │   └── client.py        # 多 Provider LLM 客户端
│   └── prompts/
│       └── assembler.py     # System prompt 组装
├── index/                   # 代码索引（AST + 正则）
├── ui/
│   └── cli.py               # CLI（原始终端 + 全键盘支持）
├── skills/                  # 技能库
├── workspace/               # Agent 工作目录
├── tests/                   # 189+ 测试
├── docs/                    # 项目文档
│   ├── gap-analysis.md      # 差距分析
│   ├── SPEC-P0.md          # P0 实施规格
│   └── code-review-issues.md
├── demo.py                  # Mock LLM 演示
├── CODING_AGENT.md          # 本项目指令文件
├── CLAUDE.md                # 开发指引
└── SPECS.md                 # 分阶段规格
```

## 开发

```bash
# 运行所有测试
pytest tests/ -q

# 跳过 prompt 测试
pytest tests/ -q -k "not prompt"

# Mock 模式演示
python demo.py
```
