# 6 个待实施 AC 的设计讨论

> 配合 `/Users/liwei/.claude/plans/floofy-skipping-corbato.md` 使用
> 状态：设计讨论中，未开始实施

每个 AC 给出：现状 / 设计选项 / 推荐 / 待用户拍板的决策点。

---

## AC-P13-2: SDD 解析器（已知失败的 1 行测试）

### 现状
- `tests/test_integration_all_tools.py:446` 调用 `verify_acs(phase_id="P0")`，但 SPECS.md 中无 `## P0` 标题（只有 `## P0` 然后里面是 `### P0-1`、`### P0-2` 等）
- 这是项目**唯一**已知失败测试
- `verify_acs()` 实现本身没问题，是 fixture 错了

### 设计选项

| 选项 | 改动 | 优点 | 缺点 |
|---|---|---|---|
| A | 改测试：`"P0"` → `"P1"` | 1 行 fix，立即恢复绿 | 不严谨，P1 测试验证的是 P1 的 AC |
| B | 改测试：删掉这个断言 | 测试文件少 1 行 | 丢失一个集成测试覆盖 |
| C | 改 `spec_loader.verify_acs` 支持 `"P0"` 前缀匹配 | 健壮性高 | 改实现，影响面大 |

### 推荐
**选项 A**：1 行改测试，与现有 SPECS.md 一致（这是真实场景，测试本就该用正确的 phase_id）。预期该测试本意是验证 P0 阶段的 AC，但 fixture 用 `"P0"` 字符串与 `## P0`（heading）不对应（实际 heading 是 `## P0: 核心体验达标 ✅`，下面子项是 `### P0-1` 等）。

需要先**读 `tests/test_integration_all_tools.py:446` 上下文**确认这个测试预期的 phase 是什么，可能是 P0 也可能是 P1——根据 SPECS.md 第 73 行 `## P0: 核心体验达标 ✅`，phase_id 可能是 `"P0"`（heading 文本里的 "P0"），那么选项 A 反而是错的，需要走选项 C 让 `verify_acs` 容忍 "P0" 作为前缀。

### 待拍板
1. 读上下文后判断 phase 期望是什么 → 决定选项 A 还是 C

---

## AC-P12-3: 任务状态机与断点续传

### 现状
- `TaskStateMachine` 完整，`transition()` 原子写 `~/.coding-agent/task_state.json`
- `engine.py:run_stream()` 主循环**不**调用 `transition()`，只在 hook 触发时被动更新
- `--resume` CLI 参数路径不完整：未读 `task_state.json` 重建上下文

### 设计选项（engine 接线方式）

| 选项 | 触发位置 | 优点 | 缺点 |
|---|---|---|---|
| X | 在 `run_stream` 显式调 `transition()` | 直接、可读 | 改动 engine.py 多处 |
| Y | 在 hook 里调（已有 `agent/hooks/task_state.py`），但**主动 emit** `after_plan` 等自定义事件 | 不动 engine 主循环 | 需要新增 11 个 hook 点之外的事件 |
| Z | 用 `enter_phase`/`exit_phase` 装饰器包住 `run_plan()`/`run_execute()`/`run_tests` | 关注点分离 | 装饰器可能干扰 async 流 |

### 推荐
**选项 X**：最简单。`run_stream` 是同步状态机，自然显式调 `transition(TaskState.PLAN/EXEC/TEST/REVIEW/DONE)`。

### 设计选项（--resume 恢复粒度）

| 选项 | 恢复内容 | 优点 | 缺点 |
|---|---|---|---|
| P | 仅恢复 `known_issues` 注入 system prompt | 简单 | 不会重跑已完成步骤 |
| Q | 恢复 ExecutionPlan + 已完成步骤列表，**跳过**已完成 | 严格续传 | 需改动 ExecutionPlan 数据结构 |
| R | 恢复 ExecutionPlan + 重跑已完成步骤（幂等性要求） | 最严谨 | 测试覆盖成本高 |

### 推荐
**选项 P**：与现状（hook-only 状态更新）兼容，迭代式演进。先把"已知问题能恢复"做掉，下一版再做"步骤级断点"。

### 设计选项（失败/中断语义）

`--resume` 启动时 `task_state.json` 是 `FAILED` 还是 `EXEC` 状态？两种语义：

| 选项 | 行为 | 适用 |
|---|---|---|
| M1 | 任何非 `DONE` 状态都 resume | 通用 |
| M2 | 只有 `EXEC/PLAN/REVIEW` 可 resume，`FAILED` 必须人工 `--retry` | 严格 |

### 推荐
**M1**（通用）。`task_state.json` 含 `last_error`，失败状态也能恢复（带错误提示）。

### 待拍板
1. engine 接线方式：X / Y / Z
2. resume 粒度：P / Q / R
3. 失败语义：M1 / M2

---

## AC-P12-4: 真实语义记忆

### 现状
- `SentenceTransformerProvider` 用 `all-MiniLM-L6-v2` (90MB)，未安装时 `ImportError`
- `get_default_provider("auto")` 当前**静默**降级到 hashing
- `sentence-transformers` 不在 `pyproject.toml`

### 设计选项（默认行为）

| 选项 | `EMBEDDING_PROVIDER=auto` 行为 | 用户感知 |
|---|---|---|
| α | 优先 SBERT，失败 → warning log + 降级 hashing | 明确 |
| β | 优先 SBERT，失败 → raise（启动失败） | 严格 |
| γ | 维持现状（静默降级） | 不可见 |

### 推荐
**α**：明确降级，CI 日志可追踪。降级不破坏功能，只损失语义搜索质量。

### 设计选项（依赖管理）

| 选项 | pyproject 改动 | 体积影响 |
|---|---|---|
| dep1 | `sentence-transformers` 进主 `dependencies`（必装） | +90MB |
| dep2 | 进 `optional-dependencies.semantic` extra（按需装） | 0 by default |
| dep3 | 不进 pyproject，文档提示 `pip install sentence-transformers` | 0 |

### 推荐
**dep2**：`pip install -e .[semantic]` 启用。90MB 不应强加给所有用户（CLI 用户大概率用不到 L3 搜索）。

### 设计选项（验收测试）

| 选项 | 测试内容 | 是否依赖 SBERT |
|---|---|---|
| t1 | 写 3 条记忆，查询"如何处理并发"，断言 SBERT 找到 asyncio | 依赖 |
| t2 | 写 3 条记忆，断言"hashing 模式下 asyncio 也在前 3"（baseline） | 不依赖 |

### 推荐
**t1+t2**：用 `@pytest.mark.skipif(not HAS_SBERT, reason=...)` 跳 SBERT 不可用；本地/CI 不装也能跑 baseline。

### 待拍板
1. 默认行为：α / β / γ
2. 依赖管理：dep1 / dep2 / dep3
3. 验收测试：t1 / t2 / t1+t2

---

## AC-P13-5: Evaluator Agent

### 现状
- `EvaluatorAgent` 完整：`_pick_alternate_model()` 跨家族判官，`to_markdown()` 输出 SCORE.md
- **仅** `/evaluate` slash command 触发；engine 主循环不调用
- 缺集成测试

### 设计选项（自动触发时机）

| 选项 | 触发点 | 用户感知 |
|---|---|---|
| E1 | 任务完成后**总是**自动 evaluate | 多 1 次 LLM 调用、~2s 延迟；总能看到 SCORE.md |
| E2 | CLI 加 `--evaluate` flag，开启才 evaluate | opt-in，零成本 |
| E3 | 配置 `auto_evaluate_on_complete: bool`（默认 False） | opt-in，可设默认开 |

### 推荐
**E2**：opt-in 最保守，避免给所有用户加成本。也可以走 E3 但需要新 config 字段。

### 设计选项（SCORE.md 位置）

| 选项 | 路径 | 优点 | 缺点 |
|---|---|---|---|
| S1 | `WORKSPACE/SCORE.md` | spec 原文要求 | 与 git 仓库混在一起 |
| S2 | `~/.coding-agent/scores/{date}.md` | 不污染 repo | spec 不要求 |

### 推荐
**S1**：与 SPECS.md 一致。

### 设计选项（缺测试覆盖）

| 选项 | 测试 | 覆盖度 |
|---|---|---|
| T1 | `test_run_with_evaluator_writes_score_md`（mock LLM，验证文件） | 高 |
| T2 | `test_evaluator_picks_alternate_model`（验证跨家族判官） | 中 |
| T3 | `test_evaluator_handles_failed_task`（异常路径） | 中 |

### 推荐
**T1+T2**：核心路径 + 关键决策点。T3 留待后续。

### 待拍板
1. 自动触发：E1 / E2 / E3
2. SCORE.md 位置：S1 / S2
3. 测试覆盖：T1+T2 / T1+T2+T3

---

## AC-P14-1: OpenTelemetry 集成

### 现状
- 3 个 shim 文件存在：`tracing.py` / `metrics.py` / `logging.py`
- OTel SDK **不在** `pyproject.toml`，三个 import 是 try/except，SDK 缺失时静默 no-op
- 默认行为是 no-op（spec 要求"默认 console fallback"）

### 设计选项（依赖管理）

| 选项 | pyproject | 启动行为 |
|---|---|---|
| O1 | 进 `optional-dependencies.observability` extra（与 P12-4 一致） | 默认 no-op + warning log |
| O2 | 进主 `dependencies` | 必装 + 总有 exporter |
| O3 | 不进 pyproject，文档提示 | 默认 no-op + warning log |

### 推荐
**O1**：与 P12-4 一致。可观测是专业用户需求，不应强加给所有用户。

### 设计选项（默认 exporter）

| 选项 | 未设 OTLP endpoint 时 | 优点 | 缺点 |
|---|---|---|---|
| X1 | `ConsoleSpanExporter`（stdout） | 立即可见、调试友好 | 长任务 stdout 噪声大 |
| X2 | no-op（仅在 SDK 装时启用） | 干净 | 用户装了 SDK 也没东西输出 |
| X3 | 写到本地文件 `~/.coding-agent/otel/{date}.jsonl` | 不污染 stdout | 又多一个文件 |

### 推荐
**X2**：与现状最兼容。装了 SDK 且设了 OTLP endpoint 才输出，否则静默；改进点是**不再静默 no-op**——SDK 缺失时 warning log 明确提示用户。

### 设计选项（warning 文案）

| 选项 | 文案 |
|---|---|
| W1 | `opentelemetry-sdk 未安装，tracing 不可用。pip install -e .[observability]` |
| W2 | 静默 no-op（维持现状） |

### 推荐
**W1**。

### 待拍板
1. 依赖：O1 / O2 / O3
2. 默认 exporter：X1 / X2 / X3
3. warning：W1 / W2

---

## AC-P14-2: Dual-agent 互审

### 现状
- `DualReviewManager(primary_chat, secondary_chat, ...)` 已接线（`engine.py:1153`）
- 但 `secondary_chat=primary_chat`（**同一个 client**）
- spec 要求"复审 Agent 来自不同 provider"

### 设计选项（备 LLM client 选择策略）

| 选项 | 选 provider 逻辑 | 优点 | 缺点 |
|---|---|---|---|
| D1 | 读 `DUAL_REVIEW_PROVIDER` env，若无则遍历 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`DASHSCOPE_API_KEY`/`MINIMAX_API_KEY` 选第一个**不同于 primary** 的 | 用户可控；自动 fallback | 跨 family 不强制 |
| D2 | 强制 OpenAI ↔ Anthropic 互审，硬要求两个 provider API key | 严格符合 spec | 单 provider 用户无法启用 |
| D3 | 保持现状（同 client），但记录 limitation 在文档 | 零改动 | 不满足 spec |

### 推荐
**D1**：最务实。单 provider 用户降级到同模型（虽不严格符合 spec 但仍能用），双 provider 用户享受跨 family 互审。

### 设计选项（启用开关）

| 选项 | 默认值 | 行为 |
|---|---|---|
| G1 | `False`（opt-in） | 默认与现状一致 |
| G2 | `True`（opt-out） | 默认启用，但需所有用户配第二 provider |

### 推荐
**G1**：opt-in 避免单 provider 用户配置爆炸。

### 设计选项（--resume 的 dual_review 联动）

dual_review 失败时，--resume 是否应"重审"已失败的步骤？还是保留原失败结果？

### 推荐
保留原结果——dual_review 是建议性，不是状态机的 source of truth。

### 待拍板
1. 备 client 选 provider：D1 / D2 / D3
2. 启用开关：G1 / G2
3. dual_review 与 --resume 联动（已推荐：保留原结果，跳过）

---

## 汇总：所有待拍板项一览

| AC | 决策点 | 推荐 | 备选 |
|---|---|---|---|
| P13-2 | 选项 A vs C | 取决于 line 446 上下文 | — |
| P12-3 | engine 接线 X/Y/Z | X | Y/Z |
| P12-3 | resume 粒度 P/Q/R | P | Q/R |
| P12-3 | 失败语义 M1/M2 | M1 | M2 |
| P12-4 | 默认行为 α/β/γ | α | γ |
| P12-4 | 依赖 dep1/2/3 | dep2 | dep3 |
| P12-4 | 测试 t1/t2 | t1+t2 | t2 |
| P13-5 | 自动触发 E1/2/3 | E2 | E3 |
| P13-5 | SCORE 位置 S1/S2 | S1 | S2 |
| P13-5 | 测试 T1/2/3 | T1+T2 | T1+T2+T3 |
| P14-1 | 依赖 O1/2/3 | O1 | O3 |
| P14-1 | exporter X1/2/3 | X2 | X3 |
| P14-1 | warning W1/W2 | W1 | W2 |
| P14-2 | provider 策略 D1/2/3 | D1 | D2 |
| P14-2 | 启用 G1/G2 | G1 | — |

**最少需要拍的决策**：12 个（去掉 P13-2 和 P14-2 联动，因为推荐已定）

可以一次性回我所有 12 个决策，或逐 AC 聊。