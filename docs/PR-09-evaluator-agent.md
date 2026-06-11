# PR-09: Evaluator Agent（独立评估器 + SCORE.md）

> 关联：SPECS.md Phase 13-5 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §9 评估器 Agent](../1.md) | [docs/参考.md 可观测性与评估 Opik/Langfuse](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 评分维度 | 4 个（完成度/代码质量/安全性/性能） | 1.md §9 列举 |
| 评分范围 | 0-10 | 与业界对齐 |
| 输出 | `SCORE.md`（人类可读）+ JSON（机器可读） | 双格式 |
| 触发时机 | 任务完成（DONE 状态）后自动触发 | 不打扰用户 |
| 独立性 | 独立 LLM（可指定不同 model） | 避免与主 agent 偏见一致 |
| 评分依据 | audit log + 代码 diff + 测试结果 | 客观证据 |

---

## 现状 / 目标

**现状**：
- 任务完成没有"质量评估"环节
- 不知道 agent 写的代码质量如何
- 没有"对完成度打分"的概念
- 用户无法客观对比"两次完成同样任务的质量"

**目标**（1.md §9）：
> **评估器 Agent**：独立的裁判 Agent，自动对任务完成度、代码质量、安全性进行多维度评分，输出 `SCORE.md`

输出示例：
```markdown
# Task Evaluation
- Task: 实现带鉴权的 API
- Agent: main
- Evaluated at: 2026-06-06T10:45:00Z

## Scores
- 完成度: 9/10
- 代码质量: 8/10
- 安全性: 7/10
- 性能: 9/10
- **总分: 8.2/10**

## Findings
- ✅ 所有 acceptance criteria 满足
- ✅ 测试覆盖 87%
- ⚠️ 缺 rate limiting
- ⚠️ `auth.py:42` 有潜在 SQL injection 风险（已用 ORM 参数化）

## 建议改进
- 添加 rate limiting 中间件
- 修复 `auth.py:42` 的输入校验
```

---

## 设计

### 数据结构

```python
# agent/agents/evaluator.py (新文件)

@dataclass
class EvaluationScore:
    dimension: str   # "completion" | "code_quality" | "security" | "performance"
    score: float     # 0-10
    rationale: str


@dataclass
class EvaluationReport:
    task: str
    agent_id: str
    scores: list[EvaluationScore]
    findings: list[str]     # 自由文本
    suggestions: list[str]
    overall_score: float
    evaluated_at: str

    @property
    def total(self) -> float:
        return self.overall_score

    def to_markdown(self) -> str: ...
    def to_json(self) -> dict: ...
```

### EvaluatorAgent

```python
# agent/agents/evaluator.py

class EvaluatorAgent:
    """独立评估器。使用不同 model 避免偏见。"""

    def __init__(self, engine: AgentEngine, model: str = None):
        self.engine = engine
        # 默认用不同 provider 的 model 做评估
        self.model = model or self._pick_alternate_model()

    def _pick_alternate_model(self) -> str:
        """选择与主 agent 不同的 model."""
        main = self.engine.config.model
        if "gpt" in main.lower():
            return "claude-sonnet-4-6"  # GPT vs Claude 互审
        if "claude" in main.lower():
            return "gpt-4o"
        return main  # fallback

    async def evaluate(
        self,
        task: str,
        agent_id: str = "main",
        audit_records: list[dict] = None,
    ) -> EvaluationReport:
        # Step 1: 收集证据
        evidence = await self._gather_evidence(task, agent_id, audit_records or [])
        # Step 2: 调 LLM 评分
        scores = await self._score(evidence)
        # Step 3: 生成报告
        report = self._build_report(task, agent_id, scores, evidence)
        return report

    async def _gather_evidence(
        self, task: str, agent_id: str, audit: list[dict]
    ) -> dict:
        """收集评估证据：audit log、git diff、测试结果."""
        evidence = {
            "task": task,
            "agent_id": agent_id,
            "tool_calls": len(audit),
            "errors": [r for r in audit if r.get("error")],
            "permission_decisions": {
                "allow": sum(1 for r in audit if r.get("permission_decision") == "allow"),
                "ask": sum(1 for r in audit if r.get("permission_decision") == "ask"),
                "deny": sum(1 for r in audit if r.get("permission_decision") == "deny"),
            },
        }
        # Git diff
        try:
            diff = subprocess.check_output(
                ["git", "diff", "--stat"], cwd=WORKSPACE, text=True
            )
            evidence["git_diff_stat"] = diff
            full_diff = subprocess.check_output(
                ["git", "diff"], cwd=WORKSPACE, text=True
            )
            evidence["git_diff"] = full_diff[:10000]  # truncate
        except subprocess.CalledProcessError:
            pass
        # Test results from audit
        test_records = [r for r in audit if r.get("tool") == "run_tests"]
        if test_records:
            evidence["last_test_result"] = test_records[-1]
        return evidence

    async def _score(self, evidence: dict) -> list[EvaluationScore]:
        """调 LLM 评分."""
        prompt = f"""\
You are an independent code quality evaluator. Score the following task
on 4 dimensions (0-10 each):

1. **completion**: Did the agent complete what was asked? Were all
   acceptance criteria met?
2. **code_quality**: Is the code clean, idiomatic, well-tested?
3. **security**: Any vulnerabilities? Input validation? Auth/authz?
4. **performance**: Any obvious bottlenecks? Algorithmic complexity?

For each dimension, give:
- A score (0-10)
- A 1-2 sentence rationale citing specific evidence

## Evidence
{json.dumps(evidence, indent=2)[:20000]}

## Output Format (JSON)
{{
  "scores": [
    {{"dimension": "completion", "score": 9, "rationale": "..."}},
    {{"dimension": "code_quality", "score": 8, "rationale": "..."}},
    ...
  ],
  "findings": ["...", "..."],
  "suggestions": ["...", "..."]
}}
"""
        resp, _ = await self.engine.llm.chat(
            [Message(role="user", content=prompt)], model=self.model
        )
        data = json.loads(resp)
        return [
            EvaluationScore(**s) for s in data["scores"]
        ], data.get("findings", []), data.get("suggestions", [])

    def _build_report(self, task, agent_id, scored, evidence):
        scores, findings, suggestions = scored
        overall = sum(s.score for s in scores) / len(scores)
        return EvaluationReport(
            task=task, agent_id=agent_id,
            scores=scores, findings=findings, suggestions=suggestions,
            overall_score=overall,
            evaluated_at=datetime.utcnow().isoformat() + "Z",
        )
```

### 输出 SCORE.md

```python
def report_to_markdown(report: EvaluationReport) -> str:
    lines = [
        "# Task Evaluation",
        f"- Task: {report.task}",
        f"- Agent: {report.agent_id}",
        f"- Evaluated at: {report.evaluated_at}",
        "",
        "## Scores",
    ]
    for s in report.scores:
        lines.append(f"- **{s.dimension}**: {s.score}/10 — {s.rationale}")
    lines.append(f"- **总分**: {report.overall_score:.1f}/10")
    lines.append("")
    lines.append("## Findings")
    for f in report.findings:
        lines.append(f"- {f}")
    lines.append("")
    lines.append("## 建议改进")
    for s in report.suggestions:
        lines.append(f"- {s}")
    return "\n".join(lines) + "\n"
```

### 触发

```python
# agent/core/engine.py 修改
# Hook: on_session_end → 自动调 Evaluator

async def _auto_evaluate(self, payload):
    if not self.config.auto_evaluate:
        return
    evaluator = EvaluatorAgent(self)
    report = await evaluator.evaluate(
        task=self.last_task,
        audit_records=self.audit.query(agent_id="main"),
    )
    # 写 SCORE.md
    score_path = WORKSPACE / "SCORE.md"
    score_path.write_text(report.to_markdown())
    # 也存 JSON
    json_path = WORKSPACE / ".score.json"
    json_path.write_text(json.dumps(report.to_json(), indent=2))
    # 用户通知
    print(f"📊 Task evaluated: {report.overall_score:.1f}/10 — see SCORE.md")
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/agents/evaluator.py` | **新建** — EvaluatorAgent + EvaluationScore + EvaluationReport |
| `agent/core/engine.py` | on_session_end hook 触发自动评估；auto_evaluate 配置 |
| `agent/core/config.py` | `auto_evaluate` (bool) + `evaluator_model` (str) |
| `agent/commands/builtin.py` | `/evaluate` 命令（手动触发） |
| `tests/test_evaluator.py` | **新建** — 评分、报告格式、跨 model 选择 |
| `tests/test_engine_evaluator.py` | **新建** — on_session_end 自动评估、SCORE.md 写入 |
| `tests/fixtures/sample_evidence.py` | **新建** — 测试用 audit 样本 |

---

## 验收标准

- [ ] `EvaluatorAgent.evaluate(task)` 返回 EvaluationReport
- [ ] 4 个维度（completion/code_quality/security/performance）各 0-10 分
- [ ] 默认 model 与主 agent 不同（GPT vs Claude 互审）
- [ ] 收集证据：audit log、git diff、test results
- [ ] 输出 `SCORE.md`（人类可读）和 `.score.json`（机器可读）
- [ ] 任务完成（DONE 状态）自动触发
- [ ] `/evaluate` 命令手动触发
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/agents/evaluator.py             (新文件，3h)
Step 2: tests/test_evaluator.py               (新文件，1.5h)
Step 3: agent/core/engine.py 集成             (改文件，1h)
Step 4: agent/core/config.py                  (改文件，0.5h)
Step 5: agent/commands/builtin.py            (改文件，0.5h)
Step 6: tests/test_engine_evaluator.py        (新文件，1h)
Step 7: tests/fixtures/sample_evidence.py     (新文件，0.5h)
Step 8: pytest tests/ 验证                    (0.5h)
```

总工作量：~8.5h

**前置依赖**：PR-01（Hook）、PR-08（Audit Log 供证据收集）

---

## 与其他 PR 的关系

- 与 PR-08 Audit Log：Evaluator 从 audit 读数据
- 与 PR-12 AB Testing：AB 实验用 Evaluator 评分对比胜出方
- 与 PR-06 SDD Parser：Evaluator 用 AC 检查完成度
- 与 PR-07 Orchestrator：Orchestrator 完成后调 Evaluator
- 与 PR-13 Progress Anchor：SCORE.md 路径记录在 progress.txt
