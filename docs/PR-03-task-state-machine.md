# PR-03: 任务状态机（INIT→PLAN→EXEC→TEST→REVIEW→DONE）

> 关联：SPECS.md Phase 12-3 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §10 长时任务与断点续传](../1.md) | [docs/参考.md 长链路任务的状态追踪](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 状态数 | 6 态（INIT/PLAN/EXEC/TEST/REVIEW/DONE） | 1.md §10 明确要求 |
| 持久化 | JSON 文件 `~/.coding-agent/task_state.json` | 进程崩溃可恢复 |
| 状态转移合法性 | 严格（不允许倒退到 PLAN 除非显式 reset） | 防止逻辑漂移 |
| 状态文件读写时机 | 每轮 tool execution 后写；每轮 LLM call 前读 | 自动且强制 |
| 与 PR-13 关系 | 共用 progress.txt（PR-13 是文本格式，本 PR 是结构化 JSON） | 互补而非替代 |

---

## 现状 / 目标

**现状**：
- 任务进度仅在内存中（`engine.history`）
- 进程崩溃 = 全部丢失
- 没有"当前在哪个 phase"概念
- 长任务（如 50 步以上）容易"失忆"——LLM 不知道前 40 步做了什么

**目标**（1.md §10）：
> 任务状态机：定义明确的任务阶段（`INIT`, `PLAN`, `EXEC`, `TEST`, `REVIEW`, `DONE`），驱动状态转移，防止逻辑漂移

- **持久化**：每轮后写 `task_state.json`，进程崩溃可恢复
- **断点续传**：`coding-agent --resume` 读 `task_state.json` 立即恢复
- **防止漂移**：强制走完 PLAN 才能进 EXEC，强制 TEST 通过才能进 REVIEW

---

## 设计

### 状态机

```python
# agent/core/task_state_machine.py (新文件)

from enum import Enum

class TaskState(Enum):
    INIT = "init"        # 任务接收
    PLAN = "plan"        # 生成执行计划
    EXEC = "exec"        # 实际执行
    TEST = "test"        # 测试验证
    REVIEW = "review"    # 代码 review / 自我评估
    DONE = "done"        # 任务完成
    FAILED = "failed"    # 任务失败

# 合法转移
ALLOWED_TRANSITIONS = {
    TaskState.INIT: {TaskState.PLAN, TaskState.FAILED},
    TaskState.PLAN: {TaskState.EXEC, TaskState.INIT, TaskState.FAILED},  # 可重新规划
    TaskState.EXEC: {TaskState.TEST, TaskState.PLAN, TaskState.FAILED},  # 失败回 PLAN
    TaskState.TEST: {TaskState.REVIEW, TaskState.EXEC, TaskState.FAILED},  # 测试失败回 EXEC
    TaskState.REVIEW: {TaskState.DONE, TaskState.EXEC, TaskState.FAILED},  # review 不通过回 EXEC
    TaskState.DONE: set(),  # terminal
    TaskState.FAILED: {TaskState.INIT, TaskState.PLAN},  # 可重试
}


@dataclass
class TaskStateRecord:
    task: str
    state: TaskState
    created_at: str
    updated_at: str
    completed_steps: list[dict] = field(default_factory=list)
    current_step: Optional[dict] = None
    next_step: Optional[dict] = None
    known_issues: list[str] = field(default_factory=list)
    op_hash: str = ""  # sha256 of last operation
    session_id: str = ""

    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, d: dict) -> "TaskStateRecord": ...


class TaskStateMachine:
    """Single source of truth for task progress."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.record: TaskStateRecord = self._load_or_init()

    def _load_or_init(self) -> TaskStateRecord:
        if self.state_file.exists():
            return TaskStateRecord.from_json(json.loads(self.state_file.read_text()))
        return TaskStateRecord(
            task="", state=TaskState.INIT,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

    def transition(self, new_state: TaskState, **kwargs) -> None:
        allowed = ALLOWED_TRANSITIONS[self.record.state]
        if new_state not in allowed:
            raise InvalidStateTransition(
                f"Cannot transition {self.record.state.value} → {new_state.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        self.record.state = new_state
        self.record.updated_at = datetime.now().isoformat()
        for k, v in kwargs.items():
            setattr(self.record, k, v)
        self._save()

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # atomic write via tmp file
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.record.to_json(), indent=2))
        tmp.replace(self.state_file)
```

### Engine 集成

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ..., session_id: str = None):
        self.task_sm = TaskStateMachine(
            state_file=Path.home() / ".coding-agent" / "task_state.json"
        )
        if session_id:
            self.task_sm.record.session_id = session_id
        # Hook: after_tool_execution → save state
        self.hooks.register("after_tool_execution", self._save_task_state)
        # Hook: before_llm_call → inject state into messages
        self.hooks.register("before_llm_call", self._inject_task_state)

    async def _save_task_state(self, payload):
        """After every tool call, persist state."""
        result = payload.get("result")
        self.task_sm.record.completed_steps.append({
            "tool": payload["tool"],
            "args": payload["args"],
            "result_hash": hashlib.sha256(str(result).encode()).hexdigest()[:16],
            "ts": time.time(),
        })
        self.task_sm._save()

    async def _inject_task_state(self, payload):
        """Before LLM call, inject current state as system reminder."""
        messages = payload["messages"]
        state_summary = (
            f"[Task State: {self.task_sm.record.state.value}]\n"
            f"Completed: {len(self.task_sm.record.completed_steps)} steps\n"
            f"Current: {self.task_sm.record.current_step}\n"
            f"Next: {self.task_sm.record.next_step}\n"
            f"Known issues: {self.task_sm.record.known_issues}"
        )
        # Append as a system-reminder (similar to cwd)
        ...
```

### 断点续传

```python
# ui/cli.py 修改

@cli.command()
@click.option("--resume", is_flag=True, help="Resume from last task_state.json")
def main(resume: bool):
    state_file = Path.home() / ".coding-agent" / "task_state.json"
    if resume and state_file.exists():
        record = TaskStateRecord.from_json(json.loads(state_file.read_text()))
        click.echo(f"Resuming task: {record.task}")
        click.echo(f"State: {record.state.value}")
        click.echo(f"Completed: {len(record.completed_steps)} steps")
        if not click.confirm("Continue?"):
            return
        engine = AgentEngine(session_id=record.session_id)
    else:
        engine = AgentEngine()
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/task_state_machine.py` | **新建** — TaskState 枚举 + TaskStateRecord + TaskStateMachine + InvalidStateTransition |
| `agent/core/engine.py` | 集成 task_sm；注册 hook：after_tool_execution（save）+ before_llm_call（inject） |
| `agent/core/__init__.py` | export TaskStateMachine |
| `ui/cli.py` | `--resume` / `--list-sessions` 参数 |
| `agent/prompts/assembler.py` | system reminder 增加 `[Task State: X]` 段 |
| `tests/test_task_state_machine.py` | **新建** — 转移合法性、持久化、并发、原子写 |
| `tests/test_engine_task_state.py` | **新建** — hook 触发、状态注入 |

---

## 验收标准

- [ ] `TaskStateMachine.transition(EXEC)` 在 INIT 状态抛 `InvalidStateTransition`
- [ ] `task_state.json` 存在时，构造函数读它；不存在时初始化为 INIT
- [ ] `_save` 原子写（tmp + replace），崩溃不损坏文件
- [ ] engine `after_tool_execution` hook 自动追加 `completed_steps`
- [ ] engine `before_llm_call` hook 自动注入状态摘要到 system-reminder
- [ ] 进程崩溃后，`coding-agent --resume` 读 `task_state.json` 恢复
- [ ] `--list-sessions` 列出 `~/.coding-agent/sessions/*.json`
- [ ] 与 PR-13 兼容：两者都写，可选使用任一
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/task_state_machine.py   (新文件，2h)
Step 2: tests/test_task_state_machine.py   (新文件，1h)
Step 3: agent/core/engine.py 集成           (改文件，1.5h)
Step 4: tests/test_engine_task_state.py     (新文件，1h)
Step 5: agent/prompts/assembler.py          (改文件，0.5h)
Step 6: ui/cli.py --resume                  (改文件，1h)
Step 7: pytest tests/ 验证                  (0.5h)
```

总工作量：~7.5h

**前置依赖**：PR-01（EventBus + Hook 必须先存在）

---

## 与其他 PR 的关系

- 与 PR-13 进度锚点：PR-13 是**人类可读**的 `claude-progress.txt`，本 PR 是**结构化**的 `task_state.json`。两者并行存在
- 与 PR-02 TDD 状态机：PR-03 管任务 phase（粗粒度），PR-02 管单个 phase 内的 TDD 循环（细粒度）
- 与 PR-07 Orchestrator：Orchestrator 内部使用 TaskStateMachine 追踪每个子任务的 phase
