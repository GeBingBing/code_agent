# PR-12: AB Testing 框架

> 关联：SPECS.md Phase 14-3 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §11 治理与持续进化](../1.md) | [docs/参考.md 持续进化 Hermes Agent](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 实验对象 | skill prompt / system prompt 段 / 工具默认参数 | 1.md §11 列举 |
| 流量分配 | 哈希 user_id → 桶 A / 桶 B | 同一用户始终同一桶 |
| 指标 | task 成功率 / token 效率 / 用户满意度 | 3 个核心 |
| 决策 | 统计显著（p < 0.05）+ 胜出方全量上线 | 简单且可解释 |
| 最小样本 | 50 个 task / 实验 | 避免过早收敛 |
| 实验存储 | `~/.coding-agent/experiments/{exp_id}/` | 与 audit 分开 |

---

## 现状 / 目标

**现状**：
- 改 prompt / 改 skill 模板直接全量上线
- 无对比数据，无法判断"新版是否更好"
- 改坏了也不知道，要等用户投诉

**目标**（1.md §11）：
> **AB 测试框架**：技能和提示词版本变更，通过 AB 测试验证效果后才全面上线

---

## 设计

### 数据结构

```python
# agent/governance/ab_test.py (新文件)

import hashlib
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum


class ExperimentStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


@dataclass
class ExperimentVariant:
    id: str                        # "A" or "B"
    name: str                      # "control" or "treatment"
    config: dict                   # 变体配置（prompt diff / skill id / tool params）
    weight: float = 1.0            # 流量权重


@dataclass
class Experiment:
    id: str
    name: str
    description: str
    target: str                    # "system_prompt" | "skill_prompt" | "tool_default"
    target_key: str                # 具体哪个段/skill/参数
    variants: list[ExperimentVariant]
    status: ExperimentStatus = ExperimentStatus.RUNNING
    created_at: str = ""
    min_samples: int = 50
    started_at: str = ""
    ended_at: str = ""
    winner: str = ""               # "A" or "B"

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "Experiment": ...


@dataclass
class ExperimentObservation:
    experiment_id: str
    variant_id: str
    user_id: str
    task: str
    success: bool
    token_input: int
    token_output: int
    duration_ms: float
    user_rating: Optional[int] = None  # 1-5
    ts: str = ""
```

### ABTestManager

```python
# agent/governance/ab_test.py

class ABTestManager:
    """AB 测试管理。流量切分 + 结果聚合。"""

    def __init__(self, exp_dir: Path = Path.home() / ".coding-agent" / "experiments"):
        self.exp_dir = exp_dir
        self.exp_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Experiment] = {}
        self._load_all()

    def _load_all(self):
        for f in self.exp_dir.glob("*/experiment.json"):
            exp = Experiment.from_dict(json.loads(f.read_text()))
            self._cache[exp.id] = exp

    def create(self, exp: Experiment) -> None:
        exp.created_at = datetime.now().isoformat()
        exp.started_at = exp.created_at
        self._save(exp)
        self._cache[exp.id] = exp

    def assign_variant(self, exp_id: str, user_id: str) -> ExperimentVariant:
        """基于 user_id 哈希分桶。"""
        exp = self._cache[exp_id]
        if exp.status != ExperimentStatus.RUNNING:
            # 已结束：返回 winner
            winner = next(v for v in exp.variants if v.id == exp.winner)
            return winner
        # 哈希分桶
        h = int(hashlib.sha256(f"{exp_id}:{user_id}".encode()).hexdigest()[:8], 16)
        h = h % 100
        cumulative = 0
        total_weight = sum(v.weight for v in exp.variants)
        for v in exp.variants:
            cumulative += (v.weight / total_weight) * 100
            if h < cumulative:
                return v
        return exp.variants[-1]

    def record_observation(self, obs: ExperimentObservation):
        """记录一次观察。"""
        log_file = self.exp_dir / obs.experiment_id / "observations.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a") as f:
            f.write(json.dumps(obs.to_dict()) + "\n")

    def analyze(self, exp_id: str) -> dict:
        """聚合分析，返回胜出方。"""
        exp = self._cache[exp_id]
        log_file = self.exp_dir / exp_id / "observations.jsonl"
        if not log_file.exists():
            return {"status": "no_data"}
        by_variant = defaultdict(list)
        for line in log_file.open():
            obs = ExperimentObservation(**json.loads(line))
            by_variant[obs.variant_id].append(obs)
        # 至少 50 样本
        for vid, obs_list in by_variant.items():
            if len(obs_list) < exp.min_samples:
                return {"status": "insufficient_samples", "have": {k: len(v) for k, v in by_variant.items()}}
        # 计算指标
        results = {}
        for vid, obs_list in by_variant.items():
            n = len(obs_list)
            results[vid] = {
                "n": n,
                "success_rate": sum(1 for o in obs_list if o.success) / n,
                "avg_tokens": sum(o.token_input + o.token_output for o in obs_list) / n,
                "avg_duration_ms": sum(o.duration_ms for o in obs_list) / n,
                "avg_rating": (
                    sum(o.user_rating for o in obs_list if o.user_rating) /
                    sum(1 for o in obs_list if o.user_rating)
                ) if any(o.user_rating for o in obs_list) else None,
            }
        # 简单胜出判定：success_rate 显著高 (>=5% 差)
        a, b = results["A"], results["B"]
        diff = b["success_rate"] - a["success_rate"]
        if abs(diff) >= 0.05:
            winner = "B" if diff > 0 else "A"
        else:
            winner = "tie"
        return {"status": "analyzed", "results": results, "winner": winner}

    def conclude(self, exp_id: str) -> Experiment:
        """结束实验，标记 winner，更新全量配置。"""
        analysis = self.analyze(exp_id)
        if analysis["status"] != "analyzed":
            return self._cache[exp_id]
        winner_id = analysis["winner"]
        exp = self._cache[exp_id]
        exp.winner = winner_id
        exp.status = ExperimentStatus.COMPLETED
        exp.ended_at = datetime.now().isoformat()
        self._save(exp)
        # 全量上线：把 winner variant 的 config 写入 active
        if winner_id in ("A", "B"):
            winner_variant = next(v for v in exp.variants if v.id == winner_id)
            self._promote_winner(exp, winner_variant)
        return exp

    def _promote_winner(self, exp: Experiment, variant: ExperimentVariant):
        """把 winner 配置写入全量配置。"""
        if exp.target == "system_prompt":
            # 更新 PromptAssembler 的某段
            ...
        elif exp.target == "skill_prompt":
            # 更新 skill 文件
            skill_path = Path("skills") / exp.target_key / "SKILL.md"
            skill_path.write_text(variant.config["new_content"])
        elif exp.target == "tool_default":
            # 更新工具默认参数
            ...

    def _save(self, exp: Experiment):
        exp_dir = self.exp_dir / exp.id
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "experiment.json").write_text(json.dumps(exp.to_dict(), indent=2))
```

### 集成到 Engine

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ...):
        self.ab_test = ABTestManager()
        # Hook: before_llm_call → 应用当前实验的 variant
        self.hooks.register("before_llm_call", self._apply_ab_variants)
        # Hook: on_session_end → 记录观察
        self.hooks.register("on_session_end", self._record_ab_observation)

    async def _apply_ab_variants(self, payload):
        """在 system prompt 应用 active experiments 的 variant。"""
        for exp in self.ab_test._cache.values():
            if exp.status != ExperimentStatus.RUNNING:
                continue
            if exp.target != "system_prompt" or exp.target_key not in self._section_map:
                continue
            variant = self.ab_test.assign_variant(exp.id, self.user_id)
            # 替换 system prompt 中的某段
            section_name = self._section_map[exp.target_key]
            payload["system_prompt"] = payload["system_prompt"].replace(
                section_name, variant.config["new_content"]
            )
            payload["_ab_experiments"] = payload.get("_ab_experiments", [])
            payload["_ab_experiments"].append({
                "exp_id": exp.id, "variant": variant.id,
            })

    async def _record_ab_observation(self, payload):
        """记录 task 完成后的观察。"""
        for exp_info in payload.get("_ab_experiments", []):
            obs = ExperimentObservation(
                experiment_id=exp_info["exp_id"],
                variant_id=exp_info["variant"],
                user_id=self.user_id,
                task=self.last_task,
                success=not payload.get("error"),
                token_input=self.total_input_tokens,
                token_output=self.total_output_tokens,
                duration_ms=(time.time() - self.task_start_ts) * 1000,
                ts=datetime.now().isoformat(),
            )
            self.ab_test.record_observation(obs)
```

### CLI 命令

```python
# agent/commands/builtin.py

@command(name="ab")
def ab_command(action: str, *args):
    if action == "list":
        ...
    elif action == "create":
        ...
    elif action == "status":
        ...
    elif action == "conclude":
        ...
    elif action == "analyze":
        ...
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/governance/__init__.py` | **新建** — governance 子包 |
| `agent/governance/ab_test.py` | **新建** — ABTestManager + Experiment + Variant + Observation |
| `agent/core/engine.py` | 集成 ab_test；2 个 hook 接入 |
| `agent/commands/builtin.py` | `/ab` 命令组（list/create/status/conclude） |
| `ui/cli.py` | `/ab` 子命令 UI |
| `tests/test_ab_test.py` | **新建** — 流量分配、聚合、winner 判定、promote |
| `tests/test_engine_ab.py` | **新建** — hook 应用 variant、记录 observation |

---

## 验收标准

- [ ] `ABTestManager.create(exp)` 保存到 `~/.coding-agent/experiments/{id}/experiment.json`
- [ ] `assign_variant(exp_id, user_id)` 基于 SHA256 哈希分桶，同一 user 始终同桶
- [ ] `record_observation(obs)` 追加到 `observations.jsonl`
- [ ] `analyze(exp_id)` 至少 50 样本才分析
- [ ] `analyze` 输出 success_rate / avg_tokens / avg_duration / avg_rating
- [ ] winner 判定：success_rate 差 ≥ 5% 才认为胜出
- [ ] `conclude(exp_id)` 标记 COMPLETED + winner，写入全量配置
- [ ] engine hook 自动应用 active variant
- [ ] engine on_session_end 自动记录观察
- [ ] `/ab` 命令支持 list / create / status / conclude
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/governance/__init__.py           (新文件，0.1h)
Step 2: agent/governance/ab_test.py            (新文件，3h)
Step 3: tests/test_ab_test.py                  (新文件，1.5h)
Step 4: agent/core/engine.py 集成                (改文件，1.5h)
Step 5: agent/commands/builtin.py              (改文件，1h)
Step 6: ui/cli.py                              (改文件，0.5h)
Step 7: tests/test_engine_ab.py                (新文件，1h)
Step 8: pytest tests/ 验证                     (0.5h)
```

总工作量：~9h

**前置依赖**：PR-01（Hook）、PR-10（OTel 提供 token/duration metrics）

---

## 与其他 PR 的关系

- 与 PR-10 OpenTelemetry：OTel 提供指标数据，AB 用其对比
- 与 PR-09 Evaluator：AB winner 用 Evaluator 评分
- 与 PR-08 Audit：实验创建/结束决策进入 audit
- 与 PR-07 Orchestrator：Orchestrator 子任务可参与 AB 实验

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/governance/ab_test.py` | `ABTestFramework` — 流量分配 + 结果聚合 |
| 适用对象 | 技能 prompt 模板、system prompt 段、工具默认参数 |
| 流量切分 | 哈希 user_id → 桶 A / 桶 B |
| 指标 | task 成功率、token 效率、用户满意度 |
| 决策 | 胜出方自动全量上线，落败方归档到 `skills/.archive/` |
