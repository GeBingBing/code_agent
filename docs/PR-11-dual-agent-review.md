# PR-11: Dual-agent 互审（高风险操作）

> 关联：SPECS.md Phase 14-2 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §8 全生命周期权限](../1.md) | [docs/参考.md 纵深防御](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 适用范围 | CRITICAL 风险（写文件/Shell/Git push/PR 创建/网络） | 1.md §8 明确 |
| Reviewer 来源 | 不同 provider（GPT vs Claude 互审） | 降低同源偏见 |
| 决策机制 | 一致 → 放行；分歧 → 上报用户 | 简单且可解释 |
| 失败处理 | 双 Agent 都否决 → 直接拒绝 | 安全优先 |
| 性能开销 | 高风险操作限流（每分钟 ≤ 5 次） | 防滥用 |
| 不适用范围 | 低风险操作（read_file/grep/list_files） | 不必要 |

---

## 现状 / 目标

**现状**：
- 高风险操作只过权限系统（permission）
- 单一 agent 决策，无交叉验证
- 复杂操作（如 `rm -rf` 加上环境变量）可能绕过 pattern matching

**目标**（1.md §8）：
> **全生命周期权限**：精细化分层授权：只读、白名单命令、用户确认、自动执行。**高风险操作需"双 Agent 互审"**

---

## 设计

### DualReviewManager

```python
# agent/core/dual_review.py (新文件)

import asyncio
from dataclasses import dataclass
from enum import Enum


class ReviewVerdict(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


@dataclass
class ReviewDecision:
    reviewer_id: str
    model: str
    verdict: ReviewVerdict
    rationale: str


@dataclass
class DualReviewResult:
    decisions: list[ReviewDecision]
    final_verdict: ReviewVerdict
    requires_user: bool
    consensus: bool


class DualReviewManager:
    """管理双 Agent 互审流程."""

    def __init__(self, primary_engine: AgentEngine, secondary_engine: AgentEngine = None):
        self.primary = primary_engine
        self.secondary = secondary_engine or self._init_secondary_engine()
        self.rate_limiter = RateLimiter(max_per_minute=5)

    def _init_secondary_engine(self) -> AgentEngine:
        """创建使用不同 model 的 secondary engine."""
        primary_model = self.primary.config.model
        alternate = "gpt-4o" if "claude" in primary_model.lower() else "claude-sonnet-4-6"
        config = AgentConfig(model=alternate)
        return AgentEngine(config=config)

    async def review(
        self,
        tool_name: str,
        args: dict,
        context: str = "",
    ) -> DualReviewResult:
        """让两个 agent 独立评估 tool call 的安全性."""
        # 速率限制
        if not self.rate_limiter.allow():
            return DualReviewResult(
                decisions=[],
                final_verdict=ReviewVerdict.REJECT,
                requires_user=True,
                consensus=True,
            )
        # 并行调两个 reviewer
        prompt = self._build_review_prompt(tool_name, args, context)
        decisions = await asyncio.gather(
            self._review_with(self.primary, prompt, "primary"),
            self._review_with(self.secondary, prompt, "secondary"),
        )
        return self._aggregate(decisions)

    def _build_review_prompt(self, tool_name: str, args: dict, context: str) -> str:
        return f"""\
You are an independent code review agent. Evaluate the following
high-risk tool call for safety:

Tool: {tool_name}
Args: {json.dumps(args, indent=2)}
Context: {context}

Consider:
- Could this cause data loss (rm -rf, drop table)?
- Could this leak secrets (env vars, credentials)?
- Could this violate security policies (SQL injection, XSS)?
- Is the path/target within expected scope?

Output JSON:
{{
  "verdict": "approve" | "reject",
  "rationale": "1-2 sentence explanation"
}}
"""

    async def _review_with(self, engine, prompt: str, reviewer_id: str) -> ReviewDecision:
        try:
            resp, _ = await engine.llm.chat(
                [Message(role="user", content=prompt)], stream=False
            )
            data = json.loads(resp)
            return ReviewDecision(
                reviewer_id=reviewer_id,
                model=engine.config.model,
                verdict=ReviewVerdict(data["verdict"]),
                rationale=data["rationale"],
            )
        except Exception as e:
            return ReviewDecision(
                reviewer_id=reviewer_id,
                model=engine.config.model,
                verdict=ReviewVerdict.ABSTAIN,
                rationale=f"Error: {e}",
            )

    def _aggregate(self, decisions: list[ReviewDecision]) -> DualReviewResult:
        """聚合两个 reviewer 的决定."""
        verdicts = [d.verdict for d in decisions]
        approves = verdicts.count(ReviewVerdict.APPROVE)
        rejects = verdicts.count(ReviewVerdict.REJECT)
        abstains = verdicts.count(ReviewVerdict.ABSTAIN)
        # 规则：任一 reject → reject；全部 approve → approve；其他 → user
        if rejects >= 1:
            final = ReviewVerdict.REJECT
            requires_user = False
        elif approves == len(decisions):
            final = ReviewVerdict.APPROVE
            requires_user = False
        else:
            # 有 abstain 或分歧
            final = ReviewVerdict.ABSTAIN
            requires_user = True
        return DualReviewResult(
            decisions=decisions,
            final_verdict=final,
            requires_user=requires_user,
            consensus=(approves == len(decisions) or rejects == len(decisions)),
        )


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self.calls = []

    def allow(self) -> bool:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 60]
        if len(self.calls) >= self.max:
            return False
        self.calls.append(now)
        return True
```

### Engine 集成

```python
# agent/core/engine.py 修改

class AgentEngine:
    HIGH_RISK_TOOLS = {"write_file", "apply_diff", "execute_command", "git_push", "create_pr", "web_fetch"}

    def __init__(self, ...):
        self.dual_review = DualReviewManager(self)
        # Hook: before_tool_execution → 高风险工具走双审
        self.hooks.register("before_tool_execution", self._maybe_dual_review)

    async def _maybe_dual_review(self, payload):
        tool_name = payload["tool"]
        if tool_name not in self.HIGH_RISK_TOOLS:
            return
        # 已经被 permission 拒绝的，跳过（避免重复）
        if not self.permissions.is_allowed(tool_name, payload["args"]):
            return
        result = await self.dual_review.review(
            tool_name, payload["args"], context=str(payload.get("context", ""))
        )
        # 记录到 audit
        self.audit.log({
            "action": "dual_review",
            "tool": tool_name,
            "decisions": [
                {"reviewer": d.reviewer_id, "model": d.model,
                 "verdict": d.verdict.value, "rationale": d.rationale}
                for d in result.decisions
            ],
            "final_verdict": result.final_verdict.value,
            "consensus": result.consensus,
        })
        if result.final_verdict == ReviewVerdict.REJECT:
            raise PermissionDenied(
                f"Dual-agent review rejected: {result.decisions[0].rationale}"
            )
        if result.requires_user:
            # 中断流程，要求用户裁决
            raise ReviewRequiresUser(
                "Dual-agent review split. Please review the decisions and confirm."
            )
```

### User 体验

CLI 在双审分歧时显示：
```
┌─ Dual-Agent Review Required ─────────────────┐
│ Tool: write_file                              │
│ Args: {path: "src/auth/login.py", ...}        │
│                                              │
│ Primary (claude-sonnet-4-6):  APPROVE        │
│   "Path is within src/, no security concern" │
│                                              │
│ Secondary (gpt-4o):           REJECT         │
│   "Missing input validation for password"   │
│                                              │
│ [1] Override and proceed                     │
│ [2] Abort                                    │
│ [3] Show full diff                           │
└──────────────────────────────────────────────┘
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/dual_review.py` | **新建** — DualReviewManager + ReviewDecision + ReviewVerdict + RateLimiter |
| `agent/core/engine.py` | 集成 dual_review；HIGH_RISK_TOOLS 集合；hook before_tool_execution |
| `agent/core/config.py` | `enable_dual_review` (bool) + `dual_review_model` (str) |
| `ui/cli.py` | 双审分歧时显示 rich panel |
| `tests/test_dual_review.py` | **新建** — 并行评估、聚合规则、速率限制、user 介入 |
| `tests/test_engine_dual_review.py` | **新建** — 高风险工具走双审、audit 记录 |

---

## 验收标准

- [ ] `DualReviewManager.review(tool, args)` 并行调两个 reviewer
- [ ] 任一 reject → 最终 reject
- [ ] 全部 approve → 最终 approve
- [ ] 分歧（approve + reject）→ requires_user = True
- [ ] secondary engine 默认用不同 provider
- [ ] 高风险工具（write_file/execute_command/git_push）走双审
- [ ] 低风险工具（read_file/grep）跳过
- [ ] 速率限制：每分钟 ≤ 5 次
- [ ] 双审决策进入 audit log
- [ ] CLI 在分歧时显示 rich panel 给用户裁决
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/dual_review.py             (新文件，2h)
Step 2: tests/test_dual_review.py             (新文件，1h)
Step 3: agent/core/engine.py 集成               (改文件，1.5h)
Step 4: agent/core/config.py                  (改文件，0.5h)
Step 5: ui/cli.py                              (改文件，1.5h)
Step 6: tests/test_engine_dual_review.py      (新文件，1h)
Step 7: pytest tests/ 验证                     (0.5h)
```

总工作量：~8h

**前置依赖**：PR-01（Hook）、PR-08（Audit 记录双审决策）

---

## 与其他 PR 的关系

- 与 PR-08 Audit：双审决策完整记录
- 与 PR-10 OpenTelemetry：双审耗时计入 OTel metrics
- 与 PR-09 Evaluator：Evaluator 评分时检查"是否走双审"
- 与 PR-07 Orchestrator：Orchestrator 高风险子任务也可走双审

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/core/dual_review.py` | `DualReviewer` — 主 Agent 提议 → 第二个独立 Agent 复审 → 一致则放行 |
| 适用范围 | CRITICAL 风险操作（写文件、Shell、Git push、PR 创建） |
| Reviewer pool | 复审 Agent 来自不同 provider（OpenAI + Anthropic 互审） |
| 失败处理 | 上报用户并附两个 Agent 的分歧分析 |
