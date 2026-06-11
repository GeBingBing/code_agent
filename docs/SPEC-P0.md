# SPEC-P0: 核心体验达标

> 关联：[gap-analysis.md](./gap-analysis.md) | 状态：待实施 | 决策：已确认

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 项目指令文件 | `CODING_AGENT.md` | 与 Claude Code 区分 |
| 文件查找范围 | 仅 workspace 根目录 | 简单明确 |
| Plan 方案 | **方案 B** — Engine 层拆分 | Plan 作为一等公民，代码层强制 |
| TDD 策略 | 引导式 | Prompt 建议但不强制，LLM 自主决定 |

---

## P0-1: CODING_AGENT.md 项目指令加载

### 设计

```
Engine.__init__()
    │
    ▼
检查 WORKSPACE/CODING_AGENT.md
    │
    ├── 存在 → 读取内容，注入 system prompt [Project context] 段
    │
    └── 不存在 → 跳过，正常启动
```

#### System Prompt 注入位置

```python
# PromptAssembler.build_system_prompt() 新增参数
project_context: str = ""

# 插入到 BASE_SYSTEM_PROMPT 之后、工具列表之前
"""
{base_prompt}

[Project context from CODING_AGENT.md]
{project_context}
---
Follow the project conventions above.

Tools:
- read_file(...)
...
"""
```

#### 实现清单

| 文件 | 改动 |
|------|------|
| `agent/prompts/assembler.py` | `build_system_prompt()` 增加 `project_context` 参数 |
| `agent/core/engine.py` | `__init__` 中读取 WORKSPACE/CODING_AGENT.md，传入 PromptAssembler |
| `ui/cli.py` | banner 显示是否加载了 CODING_AGENT.md |
| `tests/test_prompt_assembler.py` | 新增：有/无 CODING_AGENT.md 的测试 |

#### 验收标准

- [ ] workspace 下有 CODING_AGENT.md 时，system prompt 包含其内容
- [ ] 无文件时正常降级
- [ ] `/status` 显示是否有 project context

---

## P0-2: Plan-then-Execute（方案 B — Engine 层拆分）

### 核心理念

Plan 不再是 prompt 引导的"软约定"，而是 engine 层的**一等公民**。任务必须先过 plan 阶段再执行。

### 架构变更

```
Engine.run()
     │
     ├── run_plan(task)    → 生成结构化 Plan（只读工具可用）
     │       │
     │       ▼
     │   ExecutionPlan 数据结构
     │       │
     │       ▼
     │   用户确认 / 修改 Plan
     │       │
     │       ▼
     └── run_execute(plan) → 按 Plan 逐步执行
             │
             ▼
         每步完成 → 标注 status → 验证 → 下一步
```

### ExecutionPlan 数据结构

```python
# agent/core/plan.py (新文件)

@dataclass
class PlanStep:
    id: int
    description: str        # 这一步要做什么
    tool_hint: str          # 预期工具，如 "write_file"
    expected_outcome: str   # 预期结果
    status: str = "pending" # pending | in_progress | done | skipped | failed
    result: Optional[str] = None

@dataclass
class ExecutionPlan:
    task: str
    steps: List[PlanStep]
    created_at: str
    status: str = "pending"  # pending | executing | done | failed

    def to_markdown(self) -> str: ...
    def to_dict(self) -> dict: ...

    @classmethod
    def from_llm_response(cls, text: str, task: str) -> 'ExecutionPlan':
        """Parse LLM's plan output into structured ExecutionPlan."""
        ...
```

### run_plan() — 分析阶段

```python
async def run_plan(self, task: str) -> ExecutionPlan:
    """Analyze task and produce structured plan. Read-only.
    
    LLM gets a specialized system prompt for planning:
    - Can use read_file, grep, code_search, list_files
    - Cannot use write_file, execute_command, spawn_sub_agent
    - MUST output plan in structured format
    - Returns ExecutionPlan
    """
```

Plan 阶段的 system prompt 指令：
```
"You are in PLAN mode. Your job is analysis only.
- You MAY use read-only tools: read_file, grep, code_search, list_files
- You MUST NOT write, edit, or execute anything
- Output a structured plan with numbered steps
- Each step: description, expected tool, expected outcome"
```

### run_execute() — 执行阶段

```python
async def run_execute(self, plan: ExecutionPlan) -> str:
    """Execute an approved plan step by step.
    
    - Injects plan context into system prompt
    - Before each step: mark as in_progress
    - After each step: mark as done/failed, verify
    - Failed step → attempt fix vs skip vs abort (configurable)
    - Yields progress events for streaming
    """
```

### Plan 编辑

用户可以在 CLI 中手动编辑计划（类似 Claude Code 的 plan review）：
- `/plan accept` — 批准当前计划，进入执行
- `/plan edit <step> <change>` — 修改某一步
- `/plan reject` — 拒绝计划，回到对话

### 权限模式与 Plan 的交互

| 模式 | 行为 |
|------|------|
| Plan mode | `run()` → `run_plan()` → 输出 Plan → **停止** |
| Default | `run()` → `run_plan()` → 展示 Plan → **等待确认** → `run_execute()` |
| Auto | `run()` → `run_plan()` → `run_execute()` 自动衔接 |
| Bypass | 同 Auto |

### 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/plan.py` | **新** — ExecutionPlan / PlanStep 数据结构 |
| `agent/core/engine.py` | 新增 `run_plan()` / `run_execute()`；重构 `run()` 为 plan+execute 组合 |
| `agent/prompts/assembler.py` | 新增 plan 专用 system prompt；execute 阶段注入 plan context |
| `agent/commands/builtin.py` | `/plan` 增强：accept / edit / reject 子命令 |
| `ui/cli.py` | Plan 确认交互（展示 plan → 等用户输入） |
| `tests/test_plan.py` | **新** — plan 解析、plan 执行、plan mode 测试 |

#### 验收标准

- [ ] Plan mode 下 Agent 只做分析，可用 read-only 工具，不可写/执行
- [ ] `run_plan()` 返回结构化 ExecutionPlan（markdown + JSON）
- [ ] CLI 展示 plan 后等待用户确认
- [ ] `run_execute()` 按步骤顺序执行，每步更新状态
- [ ] 步骤失败时不自动跳到下一步，先尝试修复
- [ ] `/plan accept/edit/reject` 命令正常工作

---

## P0-3: TDD 闭环（引导式）

### 设计

不强制 TDD，但通过 system prompt 引导 + `run_tests` 工具让 Agent 自然倾向 TDD 流程。

#### 新增 Tool：run_tests

```python
class RunTestsTool(BaseTool):
    name = "run_tests"
    description = "Run pytest tests and return results"

    async def execute(
        self,
        path: str = "tests/",         # 测试路径
        marker: str = "",              # pytest marker 过滤
        verbose: bool = False,         # 详细输出
        **kwargs,
    ) -> ToolResult:
        """Returns: pass/fail count, failure details, coverage"""
```

返回格式：
```
Test Results: 12 passed, 2 failed, 1 error in 3.2s

FAILURES:
  tests/test_auth.py::test_login — AssertionError: expected 200, got 401
  tests/test_api.py::test_create — TypeError: 'NoneType' object is not iterable

COVERAGE: 78% (not required)
Hint: Review failures above and fix the corresponding source files.
```

#### System Prompt 注入

```
"TDD SUGGESTION — When implementing new features:
- Consider writing tests before implementation code
- Use run_tests to verify your changes don't break existing tests
- After implementing a feature, run the full test suite with run_tests
- If tests fail, analyze the output and fix the issues"
```

注意措辞：`Consider`、`Suggestion` — 引导式，不强制。

### Red → Green → Refactor（Agent 自发循环）

```
Agent 决定走 TDD 时：
     │
1.   run_tests(path="tests/test_new_feature.py")
     → 失败（期望内，功能还不存在）
     │
2.   write_file("src/new_feature.py", ...)
     │
3.   run_tests()
     → 通过
     │
4.   重构优化 → run_tests()
     → 确认不回归
     │
5.   报告结果
```

### 实现清单

| 文件 | 改动 |
|------|------|
| `agent/tools/test_runner.py` | **新** — RunTestsTool |
| `agent/tools/__init__.py` | import test_runner |
| `agent/prompts/assembler.py` | system prompt 注入 TDD 建议 |
| `tests/test_test_runner.py` | **新** — 测试 |

#### 验收标准

- [ ] `run_tests` 工具注册成功，Agent 可调用
- [ ] `run_tests(path="tests/")` 返回 pass/fail 统计和失败详情
- [ ] `run_tests(marker="unit")` 按 marker 过滤
- [ ] system prompt 包含 TDD 引导指令（非强制措辞）
- [ ] 手动验证：Agent 在实现新功能时会主动建议先写测试

---

## 实施顺序

```
Step 1: CODING_AGENT.md 加载     (2 文件改动，小，1h)
Step 2: Plan-then-Execute         (5 文件改动，大，3-4h)
Step 3: TDD 引导 + run_tests     (3 文件改动，中，1-2h)
Step 4: 端到端验证                (全部特性联调 + 新增测试)
```

每个 Step 完成后运行 `pytest` 确保 189 测试不回归。
