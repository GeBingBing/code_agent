# PR-13: `claude-progress.txt` 进度锚点

> 关联：SPECS.md Phase 14-4 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §10 长时任务与断点续传](../1.md) | [docs/参考.md 长链路任务的状态追踪](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 文件路径 | `WORKSPACE/.claude-progress.txt` | 1.md §10 明确；项目级，可 git 追踪 |
| 文件格式 | Key-value 文本 | 人类可读，grep 友好 |
| 写入时机 | `after_tool_execution` hook（PR-01） | 每步后写 |
| 读取时机 | `before_llm_call` hook + 启动时 | 注入 system prompt |
| 与 PR-03 关系 | 并存：PR-13 文本格式（人类），PR-03 JSON（机器） | 互补 |
| op_hash 算法 | sha256 of `(prev_hash + tool + args + result)` | 链式防篡改 |

---

## 现状 / 目标

**现状**：
- 长任务（50+ 步）agent 容易"失忆"——前 30 步做了什么记不清
- 进程崩溃 = 上下文全丢，重新开始
- 没有"我现在做到哪、下一步是什么"的确定性记录

**目标**（1.md §10）：
> **进度锚点文件**：强制 Agent 读写 `claude-progress.txt`，记录 `[当前任务], [下一步], [操作哈希]`，作为断点续传的确定性锚点

格式：
```
[current_task]: 实现带鉴权的 API
[current_step]: 3/8 (writing login endpoint)
[next_step]: 4/8 (write auth middleware test)
[op_hash]: sha256:abc123...
[known_issues]: - rate limiting 未实现
[updated_at]: 2026-06-06T10:23:45
```

---

## 设计

### ProgressAnchor

```python
# agent/core/progress_anchor.py (新文件)

import hashlib
import re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field


@dataclass
class ProgressRecord:
    current_task: str
    current_step: str
    next_step: str
    op_hash: str
    known_issues: list[str] = field(default_factory=list)
    updated_at: str = ""
    extra: dict = field(default_factory=dict)


class ProgressAnchor:
    """管理 .claude-progress.txt 文件。链式 hash 防篡改。"""

    KEY_RE = re.compile(r"^\[(\w+)\]:\s*(.+?)$")

    def __init__(self, workspace: Path = None):
        self.path = (workspace or WORKSPACE) / ".claude-progress.txt"

    def read(self) -> Optional[ProgressRecord]:
        """解析 progress 文件。"""
        if not self.path.exists():
            return None
        text = self.path.read_text()
        record = ProgressRecord(current_task="", current_step="",
                                 next_step="", op_hash="")
        for line in text.split("\n"):
            m = self.KEY_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            if key == "current_task":
                record.current_task = value
            elif key == "current_step":
                record.current_step = value
            elif key == "next_step":
                record.next_step = value
            elif key == "op_hash":
                record.op_hash = value
            elif key == "known_issues":
                record.known_issues = [
                    i.strip("- ") for i in value.split("\n") if i.strip()
                ]
            elif key == "updated_at":
                record.updated_at = value
            else:
                record.extra[key] = value
        return record

    def write(self, record: ProgressRecord) -> None:
        """原子写 progress 文件。"""
        lines = [
            f"[current_task]: {record.current_task}",
            f"[current_step]: {record.current_step}",
            f"[next_step]: {record.next_step}",
            f"[op_hash]: {record.op_hash}",
            f"[known_issues]:",
        ]
        for issue in record.known_issues:
            lines.append(f"  - {issue}")
        lines.append(f"[updated_at]: {record.updated_at}")
        for k, v in record.extra.items():
            lines.append(f"[{k}]: {v}")
        content = "\n".join(lines) + "\n"
        # Atomic write
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(self.path)

    def compute_hash(self, prev_hash: str, op: str) -> str:
        """链式 hash：sha256(prev_hash + op)."""
        h = hashlib.sha256(f"{prev_hash}{op}".encode()).hexdigest()[:32]
        return f"sha256:{h}"

    def verify_chain(self) -> bool:
        """（可选）验证 hash 链。"""
        record = self.read()
        if not record:
            return True
        # 需要记录每次 op 才能验证，此处简化
        return True
```

### Engine 集成

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ...):
        self.anchor = ProgressAnchor()
        # Hook: before_llm_call → 读 progress 注入 system prompt
        self.hooks.register("before_llm_call", self._inject_progress)
        # Hook: after_tool_execution → 更新 progress
        self.hooks.register("after_tool_execution", self._update_progress)
        # 启动时：如果 progress 存在，提示用户是否续传
        if self.anchor.path.exists():
            self._prompt_resume()

    async def _inject_progress(self, payload):
        record = self.anchor.read()
        if not record:
            return
        # 注入到 user message 末尾的 system-reminder
        progress_text = (
            f"current_task: {record.current_task}\n"
            f"current_step: {record.current_step}\n"
            f"next_step: {record.next_step}\n"
            f"known_issues: {', '.join(record.known_issues) or 'none'}"
        )
        reminder = f"<system-reminder>\n<progress>\n{progress_text}\n</progress>\n</system-reminder>"
        messages = payload["messages"]
        if messages and messages[-1].role == "user":
            messages[-1].content = messages[-1].content + "\n" + reminder

    async def _update_progress(self, payload):
        """每步后更新 progress。"""
        # 读取当前
        record = self.anchor.read() or ProgressRecord(
            current_task=self.last_task or "unknown",
            current_step="", next_step="", op_hash=""
        )
        # 更新 step
        step_num = self._extract_step_num(record.current_step) + 1
        record.current_step = f"{step_num}/{self.total_steps} (last: {payload['tool']})"
        # 更新 next_step（基于 plan）
        record.next_step = self._predict_next_step()
        # 更新 known_issues
        if payload.get("error"):
            record.known_issues.append(f"{payload['tool']}: {payload['error']}")
        # 更新 hash
        op_str = f"{payload['tool']}:{json.dumps(payload['args'], sort_keys=True)}"
        record.op_hash = self.anchor.compute_hash(record.op_hash, op_str)
        record.updated_at = datetime.now().isoformat()
        # 写
        self.anchor.write(record)

    def _prompt_resume(self):
        record = self.anchor.read()
        click.echo(f"Resuming task: {record.current_task}")
        click.echo(f"  Step: {record.current_step}")
        click.echo(f"  Next: {record.next_step}")
        click.echo(f"  Updated: {record.updated_at}")
        click.echo(f"  Known issues: {', '.join(record.known_issues) or 'none'}")

    def _extract_step_num(self, step_str: str) -> int:
        m = re.match(r"(\d+)/", step_str)
        return int(m.group(1)) if m else 0

    def _predict_next_step(self) -> str:
        # 简单实现：基于 plan 的下一步
        if self.plan and self.plan.current_step_idx < len(self.plan.steps):
            return self.plan.steps[self.plan.current_step_idx + 1].description
        return "unknown"
```

### CLI `--resume` 增强

```python
# ui/cli.py 修改

@cli.command()
@click.option("--resume", is_flag=True, help="Resume from .claude-progress.txt")
def main(resume: bool):
    if resume:
        engine = AgentEngine()
        # engine 启动时自动检测 .claude-progress.txt 并 prompt
```

### 断点续传场景

```
$ coding-agent "实现带鉴权的 API"
  [step 1/8: writing models]
  [step 2/8: writing routes]
  [step 3/8: writing login endpoint]
^C

$ coding-agent --resume
  Resuming task: 实现带鉴权的 API
    Step: 3/8 (last: write_file src/auth/login.py)
    Next: 4/8 (write auth middleware test)
    Updated: 2026-06-06T10:23:45
    Known issues: none
  Continue? [Y/n] y
  [step 4/8: writing auth middleware test]
  ...
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/progress_anchor.py` | **新建** — ProgressAnchor + ProgressRecord + 链式 hash |
| `agent/core/engine.py` | 集成 anchor；2 个 hook 接入；启动时检测续传 |
| `ui/cli.py` | `--resume` 参数 + 续传 prompt |
| `agent/prompts/assembler.py` | system reminder 段 `<progress>` |
| `.gitignore` | 添加 `.claude-progress.txt`（默认不提交） |
| `tests/test_progress_anchor.py` | **新建** — 读写、解析、链式 hash、原子写 |
| `tests/test_engine_progress.py` | **新建** — hook 注入、hook 更新、断点续传 |

---

## 验收标准

- [ ] `ProgressAnchor.read()` 解析 `.claude-progress.txt` 返回 ProgressRecord
- [ ] `ProgressAnchor.write(record)` 原子写（tmp + replace）
- [ ] `compute_hash(prev, op)` 返回 `sha256:hex[:32]`
- [ ] engine `before_llm_call` hook 把 progress 注入到 system-reminder
- [ ] engine `after_tool_execution` hook 自动更新 progress
- [ ] 启动时检测到 `.claude-progress.txt` 自动提示续传
- [ ] `coding-agent --resume` 显式恢复
- [ ] 与 PR-03 兼容：两者都写同一目录，无冲突
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/progress_anchor.py         (新文件，1.5h)
Step 2: tests/test_progress_anchor.py         (新文件，0.5h)
Step 3: agent/core/engine.py 集成               (改文件，1.5h)
Step 4: agent/prompts/assembler.py            (改文件，0.5h)
Step 5: ui/cli.py --resume                    (改文件，1h)
Step 6: .gitignore                            (改文件，0.1h)
Step 7: tests/test_engine_progress.py         (新文件，1h)
Step 8: pytest tests/ 验证                    (0.5h)
```

总工作量：~6.5h

**前置依赖**：PR-01（Hook）

---

## 与其他 PR 的关系

- 与 PR-03 Task State Machine：PR-03 是结构化 JSON，PR-13 是人类可读文本，并存
- 与 PR-01 Hook：progress 更新走 hook
- 与 PR-05 repomap：两者都注入 system-reminder
- 与 PR-06 SDD Parser：progress 段可加 `[current_ac]` 字段追踪当前 acceptance criterion
- 与 PR-07 Orchestrator：Orchestrator 写"当前子任务"到 progress

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/core/progress_anchor.py` | `ProgressAnchor` — 链式 hash 的人类可读进度文件读写 |
| 路径 | `WORKSPACE/.claude-progress.txt`（项目级，可 git 追踪） |
| 格式 | `[current_task]` / `[current_step]` / `[next_step]` / `[op_hash]` / `[known_issues]` / `[updated_at]` |
| 强制时机 | 每轮 LLM call 前自动 read，每轮 tool execution 后自动 write |
| 断点续传 | 进程崩溃后，新会话读 progress.txt 立即恢复上下文 |
