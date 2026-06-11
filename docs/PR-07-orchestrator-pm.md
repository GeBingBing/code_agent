# PR-07: Orchestrator PM Agent（编排-执行者模式）

> 关联：SPECS.md Phase 13-3 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §7.1 编排-执行者模式](../1.md) | [docs/参考.md 多 Agent 协作 OpenManus / MoAI-ADK](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 主 Agent 名称 | `OrchestratorAgent`（PM） | 1.md §7.1 明确 |
| 子 Agent 集群 | 4 个（Code / Test / Reviewer / DevOps） | 1.md §7.1 列举的最小集 |
| 通信协议 | EventBus（PR-01）+ TaskRequest/Response dataclass | 复用基础设施 |
| 任务拆分 | LLM-driven（Orchestrator 调 LLM 拆任务） | 简单任务不需要预先规则 |
| 调度策略 | 串行依赖 + 独立子任务并行 | 与现有 `spawn_parallel` 对齐 |
| 结果合并 | LLM-driven summary | 灵活且无需预定义 schema |

---

## 现状 / 目标

**现状**：
- `spawn_sub_agent` 串行等待，父 agent 卡死（dialog 卡）
- 没有"PM"角色，所有 agent 平等
- 复杂任务（"实现带鉴权的 API"）需要 6+ 步骤，LLM 自己规划容易遗漏

**目标**（1.md §7.1）：
- 主 Agent（PM）：负责任务分解、计划生成、进度追踪、冲突裁决
- 子 Agent 集群：
  - `Code Generator`：负责编码
  - `Test Engineer`：负责编写测试和验证
  - `Reviewer`：负责架构合规性检查
  - `DevOps`：负责环境配置、CI 交互

---

## 设计

### 数据结构

```python
# agent/agents/orchestrator.py (新文件)

@dataclass
class TaskRequest:
    """子任务请求."""
    task_id: str
    parent_task_id: Optional[str]
    role: str              # "code" | "test" | "reviewer" | "devops"
    description: str
    inputs: dict           # 上游产物
    depends_on: list[str] = field(default_factory=list)  # task_id 列表
    priority: int = 5
    timeout: int = 600     # seconds
    assigned_model: str = None  # 角色特定 model


@dataclass
class TaskResponse:
    task_id: str
    status: str            # "done" | "failed" | "timeout"
    outputs: dict
    error: Optional[str] = None
    duration: float = 0.0
    sub_tasks: list["TaskResponse"] = field(default_factory=list)
```

### 角色专业化

```python
# agent/agents/roles.py (新文件)

@dataclass
class AgentRole:
    name: str
    description: str
    system_prompt_addon: str
    preferred_model: str = None
    tools: list[str] = field(default_factory=list)


CODE_ROLE = AgentRole(
    name="code_generator",
    description="编码专家，专注于实现功能",
    system_prompt_addon="""\
You are a code generator specialist. Your job is to implement features
based on specifications. Focus on:
- Clean, idiomatic code
- Proper error handling
- Following project conventions (see CODING_AGENT.md)
- Writing minimal, focused changes

You MUST NOT modify tests, docs, or config files. Only code.
""",
    tools=["read_file", "write_file", "apply_diff", "list_files", "code_search"],
)

TEST_ROLE = AgentRole(
    name="test_engineer",
    description="测试专家，编写单元/集成测试",
    system_prompt_addon="""\
You are a test engineer. Your job is to write comprehensive tests.
Focus on:
- Edge cases
- Failure modes
- Coverage of acceptance criteria
- Following TDD (Red → Green → Refactor)

Use `run_tests` to verify your tests actually pass.
""",
    tools=["read_file", "write_file", "run_tests", "code_search"],
)

REVIEWER_ROLE = AgentRole(
    name="reviewer",
    description="代码审查专家，专注架构合规性",
    system_prompt_addon="""\
You are a code reviewer. Your job is to review changes for:
- Architecture compliance
- Security vulnerabilities
- Performance issues
- Convention violations (PEP 8, project style)

You do NOT modify code — only report findings. Use `read_file` and `grep`.
""",
    tools=["read_file", "grep", "code_search"],
)

DEVOPS_ROLE = AgentRole(
    name="devops",
    description="DevOps 专家，环境配置 + CI 交互",
    system_prompt_addon="""\
You are a DevOps specialist. Your job is to:
- Configure environments (.env, requirements.txt, Dockerfile)
- Set up CI pipelines
- Manage deployments
- Handle git operations (commit, branch, PR)

Use `execute_command` carefully — high-risk operations need user confirmation.
""",
    tools=["read_file", "write_file", "execute_command", "git_*"],
)
```

### Orchestrator 主循环

```python
# agent/agents/orchestrator.py

class OrchestratorAgent:
    """PM 角色：拆任务 → 调度子 Agent → 合并结果。"""

    def __init__(self, engine: AgentEngine):
        self.engine = engine
        self.event_bus = engine.event_bus
        self.roles: dict[str, AgentRole] = {
            "code": CODE_ROLE,
            "test": TEST_ROLE,
            "reviewer": REVIEWER_ROLE,
            "devops": DEVOPS_ROLE,
        }

    async def run(self, task: str) -> str:
        # Step 1: 拆任务（调 LLM）
        sub_tasks = await self._decompose(task)
        # Step 2: 构建依赖图
        dep_graph = self._build_dep_graph(sub_tasks)
        # Step 3: 按依赖顺序执行（DAG 调度）
        results = await self._execute_dag(sub_tasks, dep_graph)
        # Step 4: 合并结果（调 LLM）
        return await self._merge(task, results)

    async def _decompose(self, task: str) -> list[TaskRequest]:
        """调 LLM 拆任务."""
        prompt = f"""\
Decompose the following task into 3-8 subtasks. Each subtask should be:
- Atomic (one logical step)
- Assignable to a role: {list(self.roles.keys())}
- Independent or with explicit dependencies

Output JSON: [{{
  "id": "st-1",
  "role": "code",
  "description": "...",
  "depends_on": []
}}, ...]

Task: {task}
Subtasks:"""
        resp, _ = await self.engine.llm.chat([
            Message(role="user", content=prompt),
        ], stream=False)
        # Parse JSON
        return [TaskRequest(**item) for item in json.loads(resp)]

    async def _execute_dag(self, tasks, dep_graph) -> dict[str, TaskResponse]:
        """DAG 调度：按依赖顺序，并行独立任务."""
        completed = {}
        in_flight = {}
        while len(completed) < len(tasks):
            # 找出可执行的任务（依赖都已完成）
            ready = [t for t in tasks
                     if t.task_id not in completed
                     and t.task_id not in in_flight
                     and all(dep in completed for dep in t.depends_on)]
            if not ready and not in_flight:
                raise RuntimeError("Deadlock in task graph")
            # 并行执行 ready 任务
            for t in ready:
                coro = self._execute_subtask(t, completed)
                in_flight[t.task_id] = asyncio.create_task(coro)
            # 等待任意一个完成
            done, _ = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for d in done:
                # 找到对应的 task_id
                tid = next(tid for tid, c in in_flight.items() if c == d)
                completed[tid] = d.result()
                del in_flight[tid]
        return completed

    async def _execute_subtask(self, task: TaskRequest, context: dict) -> TaskResponse:
        """调对应 role 的子 agent."""
        role = self.roles[task.role]
        # 通过 event_bus 派发
        request = {
            "role": role,
            "task": task.description,
            "context": {tid: r.outputs for tid, r in context.items()},
        }
        # 等子 agent 完成（PR-01 EventBus 派发）
        future = asyncio.Future()
        async def on_complete(event):
            if event.payload["task_id"] == task.task_id:
                future.set_result(event.payload)
        queue = self.event_bus.subscribe("subagent_completed")
        asyncio.create_task(self._drain(queue, on_complete))
        # 派发
        await self.event_bus.emit("subagent_dispatched", request)
        return TaskResponse(
            task_id=task.task_id,
            status="done",
            outputs=await asyncio.wait_for(future, timeout=task.timeout),
        )

    async def _merge(self, original_task: str, results: dict) -> str:
        """调 LLM 合并所有子任务结果."""
        summary = "\n".join(
            f"[{tid}] {r.status}: {r.outputs.get('summary', '')}"
            for tid, r in results.items()
        )
        prompt = f"""\
Original task: {original_task}

Subtask results:
{summary}

Synthesize a final answer that:
1. Summarizes what was done
2. Notes any issues
3. Provides next steps
"""
        resp, _ = await self.engine.llm.chat([Message(role="user", content=prompt)], stream=False)
        return resp
```

### 集成到 Engine

```python
# agent/core/engine.py 修改

class AgentEngine:
    async def run_with_orchestrator(self, task: str) -> str:
        """复杂任务走 Orchestrator 模式."""
        orchestrator = OrchestratorAgent(self)
        return await orchestrator.run(task)
```

CLI 增加 `/orchestrate` 命令触发。

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/agents/__init__.py` | **新建** — agents 子包 |
| `agent/agents/orchestrator.py` | **新建** — OrchestratorAgent + TaskRequest/Response + DAG 调度 |
| `agent/agents/roles.py` | **新建** — 4 个 AgentRole 定义 |
| `agent/core/engine.py` | 增加 `run_with_orchestrator` 方法；通过 EventBus 派发子任务 |
| `agent/commands/builtin.py` | `/orchestrate` 命令 |
| `ui/cli.py` | Orchestrator 模式的进度展示 |
| `tests/test_orchestrator.py` | **新建** — 任务拆分、DAG 调度、并行、合并 |
| `tests/test_orchestrator_roles.py` | **新建** — 4 个 role 的 system prompt 正确 |
| `tests/test_engine_orchestrator.py` | **新建** — 端到端：简单任务走 orchestrator 拆为 3+ 子任务 |

---

## 验收标准

- [ ] `OrchestratorAgent._decompose("实现带鉴权的 API")` 返回 3-8 个 `TaskRequest`
- [ ] DAG 调度：独立子任务并行执行，依赖任务串行
- [ ] 4 个 role 注册成功：`code` / `test` / `reviewer` / `devops`
- [ ] 子 agent 通过 EventBus 派发（不阻塞父）
- [ ] `_merge` 调 LLM 生成最终报告
- [ ] 端到端：`/orchestrate 实现 JWT 鉴权` → 自动拆 6+ 子任务 → 并行/串行执行 → 合并
- [ ] 死锁检测：依赖图有环时报错
- [ ] 子任务超时控制（默认 600s）
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/agents/__init__.py             (新文件，0.1h)
Step 2: agent/agents/roles.py                (新文件，1h)
Step 3: agent/agents/orchestrator.py         (新文件，4h)
Step 4: tests/test_orchestrator.py           (新文件，2h)
Step 5: tests/test_orchestrator_roles.py     (新文件，0.5h)
Step 6: agent/core/engine.py 集成             (改文件，1.5h)
Step 7: agent/commands/builtin.py            (改文件，0.5h)
Step 8: ui/cli.py                            (改文件，0.5h)
Step 9: tests/test_engine_orchestrator.py    (新文件，1h)
Step 10: pytest tests/ 验证                   (0.5h)
```

总工作量：~11.5h

**前置依赖**：PR-01（EventBus + Hook）、PR-08（Audit Log，子 agent 行为需要审计）

---

## 与其他 PR 的关系

- 与 PR-01 EventBus：Orchestrator 通过 EventBus 派发和接收子任务
- 与 PR-08 Audit Log：所有子 agent 行为进入 audit
- 与 PR-09 Evaluator：Orchestrator 完成调 Evaluator 评分
- 与 PR-06 SDD Parser：Orchestrator 用 SDD 拆 AC 为子任务
- 与 PR-13 Progress Anchor：Orchestrator 写"当前在执行哪个子任务"到 progress.txt
