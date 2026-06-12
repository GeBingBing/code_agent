# Gap Analysis: 距离目标还有多远

> 目标：类 Claude Code 的 code agent，融合 specDD / TDD / Harness / OpenClaw / Hermes 精华
> 同步日期：2026-06-13 | 测试 1528 / 1519 passed / 1 failed | 详见 SPECS.md 文档状态 banner

---

## 完成度总览

```
████████████████░░░░ ███████████████░░░░░ ██████████████████░░░░ ████████░░░░░░░░░░░░░░
   Core Agent (85%)    specDD (75%)        TDD (90%)            Harness (40%)
████████████░░░░░░░░ ███████████░░░░░░░░░ ███████████████░░░░░
 OpenClaw (60%)        Hermes (55%)        Claude Code UX (75%)
```

**总体估算：约 60-65% 到达目标。**

---

## 一、Core Agent（已有基础 ≈85%）

| 能力 | 状态 | 差距 |
|------|------|------|
| ReAct 循环 | ✅ 已实现 | — |
| LLM 多 provider | ✅ 6 家 | provider 检测 bug 已修 |
| 文件读写/编辑 | ✅ 完整 | diff-match-patch、hunk 级别预览 |
| Shell 执行 | ✅ 已实现 | `asyncio.create_subprocess_exec`、cwd 校验、shlex.quote |
| Git 操作 | ✅ 智能操作 | commit message 生成、PR 创建（gh CLI）、branch 策略（`agent/tools/git_smart.py`） |
| 权限系统 | ✅ 4 模式 | CRITICAL 检查顺序已修 |
| 三层记忆 | ✅ 框架 | L1/L2/L3 全部就位；L3 当前为 TF-IDF 词袋模型，真实语义嵌入在 Phase 12-4 计划中（`all-MiniLM-L6-v2`） |
| 流式输出 | ✅ 支持 | typewriter 已用事件总线替换 `time.sleep`（PR-01） |
| 代码索引 | ✅ Python AST + tree-sitter | JS/TS/Go/Rust/Java 跨文件引用、调用图、语义搜索 |
| 子 Agent | ✅ 可 spawn + 角色化 | Orchestrator PM（PR-07）、Agent 专业化 |
| 沙箱 | ✅ Docker + 快照回滚 | `agent/tools/sandbox.py` |
| EventBus + Hook | ✅ 已落地 | 11 个生命周期 hook 点（`agent/core/event_bus.py`、`hooks.py`） |

---

## 二、specDD（≈75%）

specDD 的核心理念是**先写规格说明，再从规格生成代码，最后验证实现是否符合规格**。

| 能力 | 状态 | 差距 |
|------|------|------|
| SPECS.md 解析 | ✅ 已实现 | `agent/core/spec_loader.py`，正则在 `_PHASE_RE` |
| 从 SPEC 生成任务拆解 | ✅ 已实现 | `load_spec_document()` 返回 `SpecDocument` 含 AC |
| Spec → Code 生成 | ⚠️ 间接 | Agent 读 spec 写代码，但缺自动骨架生成 |
| Spec 验证 | ✅ 已实现 | `verify_acs()` / `mark_ac_done()`（PR-06） |
| Phase 追踪 | ✅ 已实现 | `load_spec_document()` 提取 phase + AC 状态 |
| Spec 变更检测 | ⚠️ 部分 | 文件 mtime 检测有，但不做 diff-based 同步 |
| E2E 规格驱动测试 | ❌ 不存在 | 没有从 spec 场景自动生成集成测试 |
| 真实语义记忆 | 📋 Phase 12-4 | 候选 `sentence-transformers all-MiniLM-L6-v2` |

**已实现的核心能力：**
- `agent/core/spec_loader.py` — 解析 SPECS.md，提取 phase、AC、状态
- `mark_ac_done()` / `verify_acs()` — AC 状态机持久化到 JSON sidecar
- 工具链：`spec_status`、`mark_ac_done`、`verify_against_spec`

---

## 三、TDD（≈90%）

TDD 精髓是 **Red → Green → Refactor** 不可跳过的状态机。

| 能力 | 状态 | 差距 |
|------|------|------|
| 写测试 | ⚠️ 引导式 | P0-3 `run_tests` 工具 + system prompt 建议先写测试（引导而非强制） |
| TDD 状态机（强制） | ✅ 已实现 | `agent/core/tdd_state_machine.py` — RED/GREEN/REFACTOR/DONE |
| Ralph 监督 | ✅ 已实现 | `agent/core/tdd_ralph.py` — 检测跳过 RED 步时强制中断 |
| 运行测试 | ✅ 已实现 | `agent/tools/test_runner.py` — pytest 集成 |
| 失败定位 | ✅ 已实现 | 解析 traceback，关联到具体代码行 |
| 自动修复 | ⚠️ 基本 | retry + 回退，根因分析有限 |
| 覆盖率分析 | ❌ 不存在 | 没有 `pytest --cov` 集成 |
| 快/慢测试分层 | ❌ 不存在 | 没有区分 unit / integration / e2e |
| Mutation testing | ❌ 不存在 | — |

**已实现的核心能力：**
- `agent/core/tdd_state_machine.py` — Red → Green → Refactor 状态机
- `agent/core/tdd_ralph.py` — Ralph 监督 Agent
- `run_tests` 工具 — pytest 集成
- 工具链：`write_failing_test` → `run_tests(expect=FAIL)` → `write_implementation` → `run_tests(expect=PASS)` → `refactor`

---

## 四、Harness（≈40%）

Harness 在工程实践中指 **CI/CD pipeline、部署编排、feature flag、金丝雀发布、可观测性**。

| 能力 | 状态 | 差距 |
|------|------|------|
| EventBus + 11 Hook 点 | ✅ 已实现 | `agent/core/event_bus.py`、`hooks.py`（PR-01） |
| 任务状态机 + 断点续传 | ⚠️ 部分 | `task_state_machine.py` + `progress_anchor.py` 已实现，`--resume` CLI 集成待完成 |
| Token 预算压缩 | ✅ 已实现 | tiktoken 集成 + 70% 目标压缩 |
| 不可变审计日志 | ✅ 已实现 | `agent/core/audit_log.py` — append-only JSONL（PR-08） |
| OpenTelemetry 集成 | ✅ 已实现 | `agent/observability/{tracing,metrics,logging}.py`（PR-10） |
| Dual-agent 互审 | ✅ 已实现 | `agent/core/dual_review.py`（PR-11） |
| AB Testing 框架 | ✅ 已实现 | `agent/governance/ab_test.py`（PR-12） |
| CI pipeline 生成 | ❌ 不存在 | 不生成 GitHub Actions / GitLab CI（手工配置） |
| Docker 构建 | ❌ 不存在 | 不生成 Dockerfile |
| 部署 | ❌ 不存在 | 不处理 deploy staging/prod |
| Feature flag | ❌ 不存在 | 没有 flag 系统 |
| 金丝雀发布 | ❌ 不存在 | — |
| 健康检查 | ❌ 不存在 | 不生成 health check endpoint |
| 告警规则 | ❌ 不存在 | — |
| Secret 管理 | ⚠️ 基本 | .env 加载，无 vault 集成 |

**结论**：可观测/治理层基本完整（Hooks + Audit + OTel + Dual Review + AB Test），CI/CD 与部署层仍是空白。

---

## 五、OpenClaw（子 Agent 系统 ≈60%）

OpenClaw 强调 **多 Agent 树形组织、生命周期管理、Agent 间通信**。

| 能力 | 状态 | 差距 |
|------|------|------|
| Agent 树形注册 | ✅ SubAgentRegistry | cleanup 孤立引用已修 |
| 深度限制 | ✅ MAX_DEPTH=5 | — |
| Spawn/Kill | ✅ 已实现 | kill 后状态覆盖已修 |
| 生命周期 | ✅ RUNNING→COMPLETED/FAILED/KILLED | — |
| Orchestrator PM | ✅ 已实现 | `agent/agents/orchestrator.py`（PR-07）— 4 角色 DAG 调度 |
| Agent 专业化 | ✅ 已实现 | CodeGenerator / TestEngineer / Reviewer / DevOps |
| 并行 Agent | ✅ 已实现 | P1-2 独立子任务并行 spawn（`agent/tools/sub_agent.py`） |
| 结果合并 | ✅ 已实现 | Orchestrator 合并多个子 agent 输出 |
| 结果回传 | ⚠️ 简单 | string 返回为主，结构化结果有限 |
| Agent 间通信 | ✅ EventBus | 通过 `TaskRequest` / `TaskResponse` dataclass 通信（PR-01 + PR-07） |
| 任务拆分策略 | ⚠️ 启发式 | Orchestrator 智能拆分，复杂任务未端到端验证 |
| 资源隔离 | ⚠️ Docker sandbox | 子 agent 共享 workspace |
| Agent 限额 | ❌ 不存在 | 无并发数限制、无 token 预算分配 |

---

## 六、Hermes（结构化推理 ≈55%）

Hermes 强调 **结构化思维、工具选择推理、chain-of-thought**。

| 能力 | 状态 | 差距 |
|------|------|------|
| Plan-then-Execute | ✅ 已实现 | `run_plan()` + `run_execute()` + ExecutionPlan（SPEC-P0） |
| 计划生成 | ✅ 已实现 | LLM 输出结构化 plan，用户确认后逐步执行 |
| 计划验证 | ⚠️ 间接 | `verify_acs()` 验证 spec 合规，缺执行后 plan 对比 |
| 工具选择推理 | ⚠️ 间接 | 工具描述详细，LLM 自行选 tool |
| 显式思考链 | ❌ 没有 | 没有 `think` phase 显式输出 |
| 结构化输出 | ✅ 已实现 | `agent/tools/structured_output.py`（JSON schema 验证） |
| 自我纠错 | ⚠️ 基本 | retry + Ralph 监督，但根因分析有限 |
| 反思机制 | ✅ 已实现 | EvolutionEngine — 失败模式学习（PR-08 / P2-1） |
| 上下文压缩策略 | ✅ 已实现 | tiktoken + 语义压缩 + 上下文优先级排序 |
| 技能提炼闭环 | ✅ 已实现 | 成功任务自动转化为 skills（`agent/core/evolution.py`） |

**已实现的核心能力：**
- Plan 阶段：先输出结构化执行计划（JSON），再逐步执行
- EvolutionEngine：失败模式学习 + 成功任务 skill 提炼
- 技能库：动态注入到 system prompt

---

## 七、Claude Code UX 对标（≈75%）

| Claude Code 特性 | 你的项目 | 差距 |
|---|---|---|
| 流式输出 | ✅ 有 | — |
| 彩色 diff | ⚠️ 有 ANSI 色 | 缺 side-by-side diff 视图 |
| Tool call 展示 | ✅ 有 | — |
| Plan 模式 | ✅ 有 | 结构化 ExecutionPlan + 用户确认 |
| Permission "Always allow" | ✅ 有缓存 | key 只认 path/command，不校验 content hash |
| /command 系统 | ✅ 11+ 命令 | /clear /plan /commit /help /model /mode /memory /status /context /review /undo /orchestrate /audit /dual-review /ab /progress /evaluate |
| CLAUDE.md 项目指令 | ✅ 有 | Engine 启动读取 workspace `CODING_AGENT.md` 注入 system prompt |
| MEMORY.md 记忆 | ✅ L3 记忆 | 持久化 + 向量索引（当前 TF-IDF，Phase 12-4 升级语义） |
| .claude/ 配置目录 | ✅ 有 | settings.json + hooks 系统（PR-01） |
| VS Code 扩展 | ✅ 有 | `extensions/vscode/`（TypeScript 实现） |
| IDE 内联补全 | ⚠️ 框架存在 | 未端到端验证 |
| 多轮对话记忆 | ✅ 已实现 | L1 工作记忆 + L2 会话摘要 |
| Task 进度条 | ✅ step 计数 | — |
| Undo/回退 | ✅ 有 | `/undo changes` / `/undo commit`（P0-5） |
| Background task | 📋 计划中 | Phase 12 / 13 |
| Conversation fork | 📋 计划中 | Phase 12 / 13 |
| Textual TUI | ✅ 基础框架 | `ui/tui.py`（P1-4），`pip install -e .[tui]` 启用 |

---

## 八、按优先级排列的 Roadmap

### 🔴 P0：核心体验达标 ✅ 已完成

1. ✅ **CODING_AGENT.md 项目指令加载** — Engine 启动时读取 workspace 根目录 CODING_AGENT.md，注入 system prompt
2. ✅ **Plan-then-execute 模式** — run_plan() + run_execute() 双阶段 API，ExecutionPlan 数据结构，CLI 确认交互
3. ✅ **TDD 引导** — run_tests 工具 + system prompt 注入 TDD suggestion
4. ✅ **Slash commands** — 11+ 命令：/clear /plan /commit /help /model /mode /memory /status /context /review /undo
5. ✅ **Undo/回退** — /undo 命令（changes / commit）

> 实现参考：`docs/SPEC-P0.md`

### 🟡 P1：显著提升能力 ✅ 已完成

6. ✅ **specDD 集成** — SPECS.md 解析 + phase 追踪 + AC 状态机
7. ✅ **子 Agent 并行执行** — 独立子任务并行跑（`agent/tools/sub_agent.py`）
8. ✅ **tree-sitter 代码索引** — Python AST + tree-sitter 跨多语言
9. ✅ **Textual TUI** — `ui/tui.py` 基础框架
10. ✅ **更好的语境管理** — tiktoken + 语义压缩 + token 预算分配

### 🟢 P2：差异化竞争力 ✅ 已完成

11. ✅ **Agent 自我进化** — EvolutionEngine + skills 提炼
12. ✅ **多文件重构感知** — 跨文件引用分析（`agent/tools/refactor.py`）
13. ✅ **Git 深度集成** — commit message 生成、PR 创建、branch 策略
14. ✅ **API/MCP 工具扩展** — `agent/mcp/{client,adapter}.py`（P2-4）

### 🔵 Phase 12: P0 Foundation ⚠️ 部分实现

15. ✅ **EventBus + 11 Hook 点** — 事件驱动核心
16. ✅ **TDD 状态机** — 强制 Red→Green→Refactor
17. ✅ **任务状态机** — INIT→PLAN→EXEC→TEST→REVIEW→DONE
18. 📋 **真实语义记忆** — 替换 TF-IDF 为 sentence-transformers

### 🟣 Phase 13: P1 Differentiation 📋 计划中

19. repomap 注入（Aider codmap 风格）
20. SDD 解析器（Acceptance Criteria 提取）
21. Orchestrator PM Agent
22. 不可变审计日志
23. Evaluator Agent

### 🟠 Phase 14: P2 Observability 📋 计划中

24. OpenTelemetry 集成
25. Dual-agent 互审
26. AB Testing 框架
27. claude-progress.txt 进度锚点

### ⚫ Phase N：按需建设

28. **Harness CI/CD** — 如果走全流程 DevOps agent 路线
29. **多模态** — 截图理解、UI 测试
30. **Code review agent** — 专门 review PR 的子 agent

---

## 结论

当前项目是一个**基础扎实、阶段性目标全部达标的 ReAct coding agent**。P0/P1/P2 三个阶段的核心能力均已实现：specDD 闭环、TDD 状态机、并行子 Agent、跨文件重构、Git 智能操作、MCP 工具扩展、自我进化引擎都已落地。修复了早期 31 个 bug 后稳定性显著提升（1519/1528 测试通过）。

下一阶段（Phase 12–14）的重点是**可观测性 + 治理**：
- **可观测**：OTel 接入、审计日志、进度锚点
- **治理**：Dual-agent 互审、A/B 测试框架、SDD AC 提取
- **差异化**：真实语义记忆（替换 TF-IDF）、Aider repomap、Evaluator Agent

与"融合 Claude Code + specDD + TDD + OpenClaw + Hermes"的愿景相比，仍有以下差距：
- **结构化推理**：缺显式 think chain、tool selection reasoning
- **Harness CI/CD**：仍空白，CI 靠手工 GitHub Actions 配置
- **多模态**：缺截图理解、UI 测试
