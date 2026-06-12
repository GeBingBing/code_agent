# PR-06: SDD 解析器（Acceptance Criteria 提取）

> 关联：SPECS.md Phase 13-2 | 状态：⚠️ 部分实现 | 决策：已确认
> 依据：[docs/1.md §4.1 规约驱动开发 SDD](../1.md) | [docs/参考.md MoAI-ADK / OpenSpec](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 解析目标 | 提取 Acceptance Criteria（AC） | 1.md §4.1 要求"符合规约的《执行计划》" |
| 格式支持 | Markdown checkbox `- [ ] <criterion>` | 与现有 SPECS.md 格式兼容 |
| 存储 | 结构化 dataclass + JSON 缓存 | 避免重复解析 |
| 工具 | `spec_status` / `mark_ac_done` / `verify_against_spec` | 与 P1-1 现有 spec 工具对齐 |
| 升级现有 | 替换/扩展 `agent/core/spec_loader.py` | 不重写 |

---

## 现状 / 目标

**现状**（Phase 10 P1-2 已有 spec_loader）：
- 解析 `## Phase X` 标题和状态标记
- 提取当前 active phase
- **不**提取 acceptance criteria
- 工具：`get_spec_status` / `mark_spec_task_done` / `verify_against_spec`

**目标**（1.md §4.1）：
- 升级解析器提取 AC
- 任务开始时强制注入"当前任务的 AC"到 system prompt
- 任务完成后自动对比"实现 vs AC"标记 gap

---

## 设计

### 数据结构

```python
# agent/core/spec_loader.py (升级)

@dataclass
class AcceptanceCriterion:
    id: str                    # e.g. "P0-3-1"
    phase_id: str              # e.g. "P0-3"
    description: str
    status: str = "pending"    # pending | in_progress | done | skipped
    verified_at: Optional[str] = None
    verified_by: Optional[str] = None  # "evaluator" | "human"

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "AcceptanceCriterion": ...


@dataclass
class SpecPhase:
    id: str                    # "P0-3"
    title: str                 # "TDD 引导"
    status: str                # ✅ / ⚠️ / 🔜
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)


@dataclass
class SpecDocument:
    phases: list[SpecPhase]
    file_path: Path
    loaded_at: str
    schema_version: str = "2.0"  # 升级标记

    def get_active_phase(self) -> Optional[SpecPhase]: ...
    def get_unfinished_acs(self, phase_id: str = None) -> list[AcceptanceCriterion]: ...
    def mark_ac_done(self, ac_id: str, verified_by: str = "human") -> None: ...
```

### 解析器

```python
# agent/core/spec_parser.py (新文件，或合并到 spec_loader)

import re

PHASE_RE = re.compile(r"^##\s+(?:Phase\s+(\d+)|P(\d+))(?:-(\d+))?:\s*(.+?)$", re.MULTILINE)
AC_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+?)$", re.MULTILINE)
SUBITEM_RE = re.compile(r"^###\s+P(\d+)-(\d+)(?:-(\d+))?:\s*(.+?)$", re.MULTILINE)


def parse_spec(md_text: str) -> SpecDocument:
    """提取 phases 和 acceptance criteria."""
    phases = {}
    current_phase = None
    current_subitem = None

    for line in md_text.split("\n"):
        # Phase 标题
        m = PHASE_RE.match(line)
        if m:
            phase_num = m.group(1) or m.group(2)
            sub_num = m.group(3)
            title = m.group(4)
            phase_id = f"P{phase_num}" + (f"-{sub_num}" if sub_num else "")
            if sub_num is None:
                # Top-level phase
                current_phase = SpecPhase(id=phase_id, title=title, status=_infer_status(md_text, line))
                phases[phase_id] = current_phase
                current_subitem = None
            else:
                # Sub-phase (e.g. P0-3)
                if current_phase is None:
                    current_phase = SpecPhase(id=f"P{phase_num}", title="(auto)", status="🔜")
                    phases[current_phase.id] = current_phase
                current_subitem = SpecPhase(id=phase_id, title=title, status="🔜")
                phases[phase_id] = current_subitem
            continue

        # Sub-item (### P0-3-1: ...)
        m = SUBITEM_RE.match(line)
        if m:
            ac_id = f"P{m.group(1)}-{m.group(2)}-{m.group(3)}"
            title = m.group(4)
            # 这是 AC 的标题，下面跟的 `- [ ]` 算 AC body
            current_ac_title = title
            current_ac_id = ac_id
            continue

        # AC line
        m = AC_RE.match(line)
        if m and current_subitem is not None:
            checked = m.group(1).lower() == "x"
            desc = m.group(2)
            ac_id = f"{current_subitem.id}-{len(current_subitem.acceptance_criteria) + 1}"
            ac = AcceptanceCriterion(
                id=ac_id,
                phase_id=current_subitem.id,
                description=desc,
                status="done" if checked else "pending",
            )
            current_subitem.acceptance_criteria.append(ac)

    return SpecDocument(
        phases=list(phases.values()),
        file_path=...,
        loaded_at=datetime.now().isoformat(),
    )
```

### 工具升级

```python
# agent/tools/spec_verifier.py 升级

class SpecStatusTool(BaseTool):
    name = "spec_status"
    description = "Return current spec status: active phase, unfinished ACs, progress %."

    async def execute(self, phase_id: str = None, **kwargs) -> ToolResult:
        spec = self.spec_doc
        active = spec.get_active_phase() if not phase_id else spec.get_phase(phase_id)
        unfinished = spec.get_unfinished_acs(phase_id)
        total = sum(len(p.acceptance_criteria) for p in spec.phases)
        done = sum(1 for p in spec.phases for ac in p.acceptance_criteria if ac.status == "done")
        progress = (done / total * 100) if total else 0
        return ToolResult(output=json.dumps({
            "active_phase": active.id if active else None,
            "unfinished_count": len(unfinished),
            "progress_pct": f"{progress:.1f}%",
            "unfinished_acs": [ac.to_dict() for ac in unfinished[:10]],
        }, indent=2))


class MarkSpecDoneTool(BaseTool):
    name = "mark_ac_done"
    description = "Mark an acceptance criterion as done."

    async def execute(self, ac_id: str, **kwargs) -> ToolResult:
        self.spec_doc.mark_ac_done(ac_id, verified_by="agent")
        self._save_cache()
        return ToolResult(output=f"AC {ac_id} marked done.")


class VerifyAgainstSpecTool(BaseTool):
    name = "verify_against_spec"
    description = "Compare implementation vs spec AC. Returns gap report."

    async def execute(self, phase_id: str, **kwargs) -> ToolResult:
        unfinished = self.spec_doc.get_unfinished_acs(phase_id)
        if not unfinished:
            return ToolResult(output=f"All ACs in {phase_id} are done.")
        return ToolResult(output=json.dumps({
            "phase": phase_id,
            "unfinished": [ac.to_dict() for ac in unfinished],
            "recommendation": "Implement these ACs to complete the phase.",
        }, indent=2))
```

### Engine 集成

```python
# agent/core/engine.py 集成

class AgentEngine:
    def __init__(self, ...):
        self.spec = self._load_spec()
        # 注入到 system prompt
        spec_context = self._build_spec_context()
        # 通过 prompt assembler
        # Hook: before_llm_call → 注入当前 phase 的 AC
        self.hooks.register("before_llm_call", self._inject_spec_ac)

    async def _inject_spec_ac(self, payload):
        active = self.spec.get_active_phase()
        if not active:
            return
        unfinished = [ac for ac in active.acceptance_criteria if ac.status == "pending"]
        if not unfinished:
            return
        # 在 user message 末尾追加 system-reminder
        ac_text = "\n".join(f"- [ ] {ac.description} (id: {ac.id})" for ac in unfinished[:5])
        reminder = f"<system-reminder>\n<spec_ac>\n{ac_text}\n</spec_ac>\n</system-reminder>"
        ...
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/spec_loader.py` | 升级：增加 AcceptanceCriterion / SpecPhase / SpecDocument dataclass；增加 parse_spec() |
| `agent/core/spec_parser.py` | **新建**（或合并到 spec_loader）— 正则解析逻辑 |
| `agent/tools/spec_verifier.py` | 升级：spec_status 增加 AC 列表；mark_ac_done 工具名对齐；增加 verify_against_spec |
| `agent/tools/__init__.py` | 注册新工具名 |
| `agent/core/engine.py` | 集成 spec 解析；hook before_llm_call 注入 AC |
| `agent/prompts/assembler.py` | spec_context 增加 AC 段 |
| `tests/test_spec_parser.py` | **新建** — 解析 Phase 标题、AC 行、嵌套 sub-item |
| `tests/test_spec_tools.py` | **新建** — 工具返回值、mark_ac_done 持久化 |
| `tests/test_engine_spec.py` | **新建** — hook 注入、未完成 AC 提示 |

---

## 验收标准

- [ ] `parse_spec(SPECS.md)` 返回所有 phase 和 AC，结构化
- [ ] `- [ ]` 解析为 `status="pending"`，`- [x]` 解析为 `status="done"`
- [ ] `spec_status` 返回 active phase + unfinished ACs + progress %
- [ ] `mark_ac_done(ac_id)` 持久化到 JSON 缓存
- [ ] `verify_against_spec(phase_id)` 返回未完成 AC 列表
- [ ] engine `before_llm_call` hook 把未完成 AC 注入到 system-reminder
- [ ] 缓存：spec 文件 mtime 变化才重解析
- [ ] 现有 `get_spec_status` / `mark_spec_task_done` 旧接口仍可用
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/spec_parser.py            (新文件，2h)
Step 2: agent/core/spec_loader.py 升级       (改文件，1.5h)
Step 3: tests/test_spec_parser.py            (新文件，1h)
Step 4: agent/tools/spec_verifier.py 升级    (改文件，1.5h)
Step 5: tests/test_spec_tools.py             (新文件，0.5h)
Step 6: agent/core/engine.py 集成             (改文件，1h)
Step 7: agent/prompts/assembler.py           (改文件，0.5h)
Step 8: tests/test_engine_spec.py            (新文件，0.5h)
Step 9: pytest tests/ 验证                    (0.5h)
```

总工作量：~9h

**前置依赖**：无（但 PR-01 Hook 系统能让实现更优雅）

---

## 与其他 PR 的关系

- 与 PR-05 repomap：repomap 注入代码结构，本 PR 注入规格结构
- 与 PR-09 Evaluator：Evaluator 调用 `verify_against_spec` 做评分
- 与 PR-07 Orchestrator：Orchestrator 用 SDD 解析结果拆分任务
- 与 PR-13 progress anchor：progress.txt 写"当前 AC"段

---

## 实现参考

> ⚠️ 当前为**部分实现**：`AcceptanceCriterion` dataclass 与 `_AC_RE` 正则解析已就位，`load_spec_document()` 返回 `SpecDocument` 含 AC 列表、`mark_ac_done()` 持久化到 JSON sidecar。但 `verify_acs(phase_id="P0")` 形式的 id 解析未对齐到现有 `P0-1` / `P0-2` 子阶段命名（详见 `tests/test_integration_all_tools.py` 失败用例、SPECS.md 文档状态 banner）。

| 文件 | 关键符号 |
|------|----------|
| `agent/core/spec_loader.py` | `AcceptanceCriterion` dataclass、`ACSpecPhase`、`load_spec_document()` 返回 `SpecDocument` |
| 正则 | `_PHASE_RE`（Phase 标题）、`_AC_RE`（`- [ ]` / `- [x]` AC 行） |
| 工具 | `mark_ac_done(ac_id)`、`verify_acs(phase_id)`、`get_spec_status()` |
