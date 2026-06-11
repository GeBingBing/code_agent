# PR-02: TDD 状态机（强制 Red→Green→Refactor）

> 关联：SPECS.md Phase 12-2 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §4.2 测试驱动开发强制循环](../1.md) | [docs/参考.md MoAI-ADK Ralph 引擎模式](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| TDD 强制 vs 引导 | **强制** | 1.md §4.2 要求；与 Phase 9 P0-3 引导式共存，引导式为默认 |
| Ralph 引擎位置 | 独立模块 `tdd_ralph.py` | 与状态机解耦，可独立测试 |
| 跳过 RED 步处理 | 强制中断 + 提示 | 1.md §4.2 要求"不可跳过" |
| 工具链 | `write_failing_test` / `run_tests` / `write_implementation` | 复用现有 `run_tests` 工具 |
| 状态持久化 | 内存（单任务内） | 任务结束状态机清零 |

---

## 现状 / 目标

**现状**（Phase 9 P0-3）：
- system prompt 注入"TDD suggestion"
- LLM 自主决定是否走 TDD
- 完全没有状态追踪，LLM 可以直接写代码不写测试

**目标**（1.md §4.2）：
```yaml
# workflow/tdd_cycle.yaml
name: TDD Cycle
steps:
  - id: write_test
    skill: test_generator
    output: "failing test"
  - id: run_test_red
    skill: test_runner
    expect: "FAIL"
  - id: write_code
    skill: code_generator
    depends_on: [run_test_red]
  - id: run_test_green
    skill: test_runner
    expect: "PASS"
  - id: refactor
    skill: refactor_tool
    depends_on: [run_test_green]
  - id: final_validation
    skill: linter
    depends_on: [refactor]
```

- 状态机：`RED → GREEN → REFACTOR → DONE` 四态
- Ralph 监督 Agent：检测到跳过时**强制介入**
- 工具调用必须按顺序，跳过则阻断

---

## 设计

### 状态机

```python
# agent/core/tdd_state_machine.py (新文件)

from enum import Enum

class TDDState(Enum):
    RED = "red"            # 写失败测试
    GREEN = "green"        # 写实现让测试通过
    REFACTOR = "refactor"  # 重构优化
    DONE = "done"

# 合法状态转移
TRANSITIONS = {
    TDDState.RED: TDDState.GREEN,
    TDDState.GREEN: TDDState.REFACTOR,
    TDDState.REFACTOR: TDDState.DONE,
    TDDState.DONE: None,  # terminal
}

@dataclass
class TDDCycle:
    """Single TDD cycle for one feature/AC."""
    feature: str
    state: TDDState = TDDState.RED
    test_path: Optional[str] = None
    test_red_run: Optional[TestResult] = None
    impl_path: Optional[str] = None
    test_green_run: Optional[TestResult] = None
    refactor_commit: Optional[str] = None


class TDDStateMachine:
    """Enforces Red→Green→Refactor sequence. Raises InvalidTransition on skip."""

    def __init__(self, tdd_mode: str = "strict"):
        """tdd_mode: 'strict' (强制), 'guided' (引导, 默认), 'off' (关闭)"""
        self.mode = tdd_mode
        self.cycle: Optional[TDDCycle] = None

    def start(self, feature: str) -> None:
        self.cycle = TDDCycle(feature=feature)

    def transition(self, next_state: TDDState) -> None:
        if self.mode == "off":
            return
        current = self.cycle.state
        allowed = TRANSITIONS[current]
        if next_state != allowed:
            if self.mode == "strict":
                raise InvalidTransition(
                    f"Cannot skip from {current.value} to {next_state.value}. "
                    f"Expected: {allowed.value}"
                )
            # guided: warn but allow
            logger.warning("TDD skip: %s → %s", current.value, next_state.value)
        self.cycle.state = next_state
```

### Ralph 监督 Agent

```python
# agent/core/tdd_ralph.py (新文件)

class RalphSupervisor:
    """MoAI-ADK Ralph 引擎模式：检测状态机违规并强制介入。

    Hooks into 'before_tool_execution' (PR-01) — observes every tool call
    and enforces TDD sequence when 'strict' mode is on.
    """

    def __init__(self, state_machine: TDDStateMachine):
        self.sm = state_machine

    async def check(self, tool_name: str, args: dict) -> Optional[str]:
        """Return error message if tool call violates TDD sequence. None if OK."""
        if self.sm.mode != "strict" or self.sm.cycle is None:
            return None

        # Rule 1: Cannot write implementation before RED step
        if tool_name in ("write_file", "apply_diff", "insert_after_line"):
            target = args.get("path", "")
            if self._is_implementation(target) and self.sm.cycle.state == TDDState.RED:
                return (
                    f"TDD violation: Cannot write implementation '{target}' "
                    f"in RED state. First write a failing test using "
                    f"'write_failing_test' or use 'run_tests' to confirm RED."
                )

        # Rule 2: Cannot refactor before GREEN step passes
        if tool_name == "refactor" and self.sm.cycle.state != TDDState.GREEN:
            return f"TDD violation: refactor only allowed in REFACTOR state."

        return None

    def _is_implementation(self, path: str) -> bool:
        return not path.startswith("tests/") and path.endswith(".py")
```

### 工具扩展

```python
# agent/tools/test_runner.py 增加新工具

class WriteFailingTestTool(BaseTool):
    """辅助 LLM 写一个 expected-to-fail 的测试。"""

    name = "write_failing_test"
    description = "Write a test that is EXPECTED to fail (TDD RED step)"

    async def execute(self, path: str, test_code: str, **kwargs) -> ToolResult:
        # 1. write test file
        # 2. run pytest
        # 3. assert result is FAIL
        # 4. return test failure details
        ...
```

### 集成到 Engine

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ..., tdd_mode: str = "guided"):
        self.tdd_sm = TDDStateMachine(tdd_mode=tdd_mode)
        self.ralph = RalphSupervisor(self.tdd_sm)
        # Register Ralph check on before_tool_execution hook
        self.hooks.register("before_tool_execution", self._ralph_check)

    async def _ralph_check(self, payload):
        tool_name, args = payload["tool"], payload["args"]
        err = await self.ralph.check(tool_name, args)
        if err:
            raise TDDViolation(err)
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/tdd_state_machine.py` | **新建** — TDDState 枚举 + TDDCycle + TDDStateMachine + InvalidTransition 异常 |
| `agent/core/tdd_ralph.py` | **新建** — RalphSupervisor |
| `agent/tools/test_runner.py` | 增加 `WriteFailingTestTool` |
| `agent/tools/__init__.py` | 注册 `write_failing_test` |
| `agent/core/engine.py` | 集成 tdd_sm + ralph；mode 字段来自 config |
| `agent/core/config.py` | 增加 `tdd_mode` 配置项（`strict` / `guided` / `off`） |
| `agent/prompts/assembler.py` | system prompt 注入 TDD 状态机说明（strict 模式下） |
| `tests/test_tdd_state_machine.py` | **新建** — 状态转移、跳过检测、strict 模式抛异常 |
| `tests/test_tdd_ralph.py` | **新建** — Ralph 拦截规则 |
| `tests/test_engine_tdd.py` | **新建** — 端到端：strict 模式下跳过 RED 步被阻断 |

---

## 验收标准

- [ ] `TDDStateMachine` 阻止 `RED → REFACTOR` 的非法转移
- [ ] strict 模式：`InvalidTransition` 异常被抛出
- [ ] guided 模式：跳过时输出 warning 但不阻断
- [ ] off 模式：完全关闭
- [ ] `RalphSupervisor.check("write_file", {"path": "src/x.py"})` 在 RED 状态下返回违规消息
- [ ] `RalphSupervisor.check("write_file", {"path": "tests/test_x.py"})` 不违规
- [ ] engine 在 strict 模式下，违规 tool_call 抛 `TDDViolation` 给 LLM 重试
- [ ] `write_failing_test` 工具：写测试 + 跑测试 + 返回失败详情
- [ ] 端到端：LLM 跳过 RED 步直接写实现时，被 Ralph 强制要求"先写一个失败测试"
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/tdd_state_machine.py   (新文件，1.5h)
Step 2: agent/core/tdd_ralph.py           (新文件，1.5h)
Step 3: tests/test_tdd_state_machine.py   (新文件，0.5h)
Step 4: tests/test_tdd_ralph.py           (新文件，0.5h)
Step 5: agent/tools/test_runner.py 扩展    (改文件，1h)
Step 6: agent/core/engine.py 集成         (改文件，1h)
Step 7: agent/core/config.py tdd_mode     (改文件，0.5h)
Step 8: agent/prompts/assembler.py        (改文件，0.5h)
Step 9: tests/test_engine_tdd.py          (新文件，1h)
Step 10: pytest tests/ 验证                (0.5h)
```

总工作量：~8.5h

**前置依赖**：PR-01（EventBus + Hook 必须先存在）

---

## 与其他 PR 的关系

- 区别于 Phase 9 P0-3 引导式 TDD：P0-3 是 prompt 引导（可忽略），PR-02 是**状态机强制**（不可跳过）
- 与 PR-03 任务状态机互补：PR-03 管任务 phase（INIT→...→DONE），PR-02 管单个 phase 内的 TDD 微循环
- 与 PR-09 Evaluator Agent：Evaluator 评分时检查"是否走完 TDD 循环"作为质量指标
