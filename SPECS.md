> **文档状态**：最后与代码同步 2026-06-13 | 测试收集 1528 个，当前 1519 passed / 1 failed / 6 skipped / 2 xfailed
> **注意**：本文档同时被人类和 `agent/core/spec_loader.py` 解析，修改时请保持 `## Phase N` / `### P0-1` 格式稳定。

# Coding Agent - SPECS.md

## 项目概述

构建一个可控、自进化的 AI 编程智能体，参考 smolagents / aider / Claude Code 等开源项目，分阶段迭代交付。

---

## Phase 0–8: 基础建设（已完成）

### Phase 0: 最小可运行原型 ✅

- LLM 客户端（OpenAI 兼容，6 providers）
- 工具注册表（read/write/execute）
- ReAct 主循环（Think → Act → Observe → Repeat）
- 简单 CLI

### Phase 1: 记忆系统 ✅

- L1: 工作记忆（对话历史，自动裁剪）
- L2: 会话摘要（超过阈值时压缩）
- L3: 长期记忆（~/.coding-agent/memory.md + SQLite 向量存储）

### Phase 2: 技能系统 ✅

- 技能文件格式（Markdown + YAML）
- create_skill / list_skills / search_skills
- 技能检索与激活

### Phase 3: 权限与安全 ✅

- 四种模式（plan / default / auto / bypass）
- CRITICAL 操作在所有模式下拦截
- 路径规则（allow/deny）
- 用户确认 + "always allow" 缓存

### Phase 4: 精确编辑 ✅

- apply_diff / insert_after_line / replace_lines
- 文件读写增强

### Phase 5: 子 Agent 与多智能体 ✅

- spawn_sub_agent / list_sub_agents / kill_sub_agent
- 树形嵌套（最大深度 5 层）
- 生命周期管理（RUNNING → COMPLETED / FAILED / KILLED）

### Phase 6: 代码索引与检索 ✅

- ✅ Python AST + 多语言 regex 符号提取
- ✅ 混合检索（文件名 + 符号 + 内容匹配）
- ✅ tree-sitter（支持 JS/TS/Go/Rust/Java，优雅降级）
- ✅ 语义搜索（find_references + build_call_graph + get_related_symbols）

### Phase 7: 沙箱与隔离 ✅

- ✅ Docker 容器执行
- ✅ 文件系统快照 / 回滚
- ✅ shlex.quote 命令注入防护

### Phase 8: CLI 增强 ⚠️

- ✅ 流式输出 + 实时 tool call 展示
- ✅ 全键盘支持（方向键、Ctrl+A/E/B/F/W/U/K、历史导航）
- ✅ CJK 双宽度字符光标定位
- ❌ Textual TUI（计划中）

---

## P0: 核心体验达标 ✅

> 旧 Phase 9 | 详细规格: [docs/SPEC-P0.md](docs/SPEC-P0.md)

- [x] `AC-P0-1` CODING_AGENT.md 项目指令加载与注入
- [x] `AC-P0-2` Plan-then-Execute 流程闭环
- [x] `AC-P0-3` TDD 引导与 run_tests 工具
- [x] `AC-P0-4` Slash 命令系统
- [x] `AC-P0-5` Undo/回退能力

### P0-1: CODING_AGENT.md 项目指令加载 ✅

- Engine 启动时读取 WORKSPACE/CODING_AGENT.md
- 注入到 system prompt [Project context] 段
- `/status` 显示加载状态

### P0-2: Plan-then-Execute ✅

- `run_plan()` — 分析阶段（PLAN 权限 + 只读工具），返回 ExecutionPlan
- `run_execute()` / `run_stream(task, plan_context)` — 执行阶段
- CLI 确认交互（y/n/edit step）
- `/plan accept/reject/edit/show` 命令

### P0-3: TDD 引导 ✅

- `run_tests` 工具（pytest 集成）
- System prompt 注入 TDD suggestion
- 返回 pass/fail/errors + 失败详情

### P0-4: Slash 命令系统 ✅

- 11 个命令：`/clear /plan /commit /help /model /mode /memory /status /context /review /undo`
- CommandRegistry 可插拔架构
- CLI `/` 前缀检测 + 路由

### P0-5: Undo/回退 ✅

- `/undo changes` — 撤销未暂存修改
- `/undo commit` — 撤销最近一次提交

---

## P1: 显著提升能力 ⚠️

> 旧 Phase 10 | 详细规格: 见 `docs/SPEC-P1.md` | [docs/SPEC-ARCHITECTURE.md](docs/SPEC-ARCHITECTURE.md)

- [x] `AC-P1-1` specDD 解析与 Phase 追踪
- [x] `AC-P1-2` 子 Agent 并行执行
- [x] `AC-P1-3` tree-sitter 代码索引（含跨文件引用）
- [x] `AC-P1-4` Textual TUI 基础框架
- [x] `AC-P1-5` 语境管理与 Token 预算压缩

### P1-1: specDD 集成

- [x] SPECS.md 解析 → 任务拆解
- [x] Phase 追踪：Agent 知道自己当前在哪个 phase
- [x] 实现 vs 规格验证
- [x] spec 工具：get_spec_status / mark_spec_task_done / verify_against_spec

### P1-2: 子 Agent 并行执行

- [x] 独立子任务并行 spawn
- [x] 结果合并
- [x] Agent 专业化（通过 model 参数和独立 config）

### P1-3: tree-sitter 代码索引

- [x] 可选 tree-sitter 解析（JS/TS/Go/Rust/Java）
- [x] 无 tree-sitter 时退回 regex
- [x] 跨文件引用分析（find_references）
- [x] 调用图构建（build_call_graph）
- [x] 语义搜索（semantic_search，包含相关符号和引用）

### P1-4: Textual TUI

- [x] 基础 TUI 框架
- [x] --tui 参数 + 降级到 CLI
- [x] Rich 组件：面板、进度条、语法高亮
- [x] 更好的 diff 展示

### P1-5: 语境管理增强

- [x] tiktoken 集成
- [x] Token 预算压缩（70% 目标）
- [x] system 消息保留
- [x] 大输出截断（>200 行 / >500 字符）
- [x] tool_call/tool_result 成对保留
- [x] 语义压缩（基于嵌入向量的智能摘要）
- [x] 上下文优先级排序（按相关性加权）

---

## P2: 差异化竞争力 ⚠️

> 旧 Phase 11 | 详细规格: 见 `docs/SPEC-P1.md` / `docs/PR-05..PR-09`

- [x] `AC-P2-1` Agent 自我进化（EvolutionEngine）
- [x] `AC-P2-2` 多文件重构感知
- [x] `AC-P2-3` Git 深度集成（commit/PR/branch 策略）
- [x] `AC-P2-4` MCP 工具扩展

### P2-1: Agent 自我进化 ✅

- [x] 成功任务自动转化为 skills
- [x] 失败模式学习
- [x] EvolutionEngine 集成到 AgentEngine
- [x] 失败上下文注入 system prompt
- [x] 11 项测试覆盖

### P2-2: 多文件重构感知 ✅

- [x] 跨文件引用分析（基于 code_indexer）
- [x] 安全的批量重命名（safe_rename + get_refactor_preview）
- [x] dry_run 预览模式
- [x] Python 语法验证
- [x] 作用域限制（path 参数）
- [x] 10 项测试覆盖

### P2-3: Git 深度集成 ✅

- [x] 智能 commit message 生成（基于 diff 的 LLM 分析）
- [x] PR 创建（gh CLI 集成，自动标题/描述）
- [x] Branch 策略（根据任务描述自动生成分支名）

### P2-4: MCP 工具扩展 ✅

- [x] Model Context Protocol 集成（JSON-RPC over stdio）
- [x] MCPToolAdapter（将 MCP 工具桥接为 BaseTool）
- [x] MCPServerManager（多服务器生命周期管理）
- [x] register_mcp_tools_from_config（配置驱动注册）
- [x] AgentEngine 集成（延迟初始化 + shutdown）
- [x] 环境变量展开（$VAR 在 env 配置中）
- [x] 40 项测试覆盖

---

## 里程碑

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | Phase 0–1：核心 Agent + 记忆 | ✅ |
| M2 | Phase 2–3：技能 + 权限安全 | ✅ |
| M3 | Phase 4–5：精确编辑 + 子 Agent | ✅ |
| M4 | Phase 6–8：代码索引 + 沙箱 + CLI | ⚠️ |
| M5 | Phase 9：P0 核心体验达标 | ✅ |
| M6 | Phase 10：P1 显著提升能力 | ⚠️ |
| M7 | Phase 11：P2 差异化竞争力 | ⚠️ |
| M8 | Phase 12：P0 Foundation（七层架构核心） | ⚠️ |
| M9 | Phase 13：P1 Differentiation（差异化竞争力） | 📋 |
| M10 | Phase 14：P2 Observability（可观测与治理） | 📋 |

---

## Phase 12: P0 Foundation — 补齐七层架构核心 ⚠️

> 详细规格：见 `docs/PR-01..PR-04` 四份 PR 文档
> 依据：[docs/1.md §3 核心 Harness 引擎设计](../docs/1.md) | [docs/参考.md 工程骨架](../docs/参考.md)

- [x] `AC-P12-1` EventBus + Hook 系统
- [ ] `AC-P12-2` TDD 状态机（强制 Red→Green→Refactor）
- [ ] `AC-P12-3` 任务状态机与断点续传
- [ ] `AC-P12-4` 真实语义记忆

### P12-1: EventBus + Hook 系统

- 核心循环从「`if/else` 直调」改为**事件驱动 + Hook 注入**
- 新增 `agent/core/event_bus.py` — 简单 pub/sub（asyncio.Queue + emit/on）
- 新增 `agent/core/hooks.py` — 11 个生命周期 Hook 点（`before_perceive` / `before_llm_call` / `after_llm_call` / `before_tool_execution` / `after_tool_execution` / `before_act` / `after_act` / `on_error` / `before_compact` / `after_compact` / `on_session_end`）
- `AgentEngine.run_stream` 重构为：`emit("before_llm_call") → llm.chat() → emit("after_llm_call") → for tool in calls: emit("before_tool_execution") → ...`
- 消除 `engine.py` 内硬编码的 `time.sleep` 流式逻辑（改为 `on_token` Hook）

### P12-2: TDD 状态机（强制 Red→Green→Refactor）

- 区别于 Phase 9 P0-3 引导式 TDD，**强制**为不可跳过的状态机
- 新增 `agent/core/tdd_state_machine.py` — `TDDState`（RED / GREEN / REFACTOR / DONE）
- Ralph 监督 Agent：检测到 LLM 跳过 RED 步直接写实现时，**强制中断**并要求"先写一个失败测试"
- 工具链：`write_failing_test` → `run_tests(expect=FAIL)` → `write_implementation` → `run_tests(expect=PASS)` → `refactor` → `run_tests(expect=PASS)`
- 借鉴 `MoAI-ADK` Ralph 引擎模式

### P12-3: 任务状态机（INIT→PLAN→EXEC→TEST→REVIEW→DONE）

- 解决 1.md §10「长链路任务状态追踪」问题
- 新增 `agent/core/task_state_machine.py` — `TaskState` 枚举 + 状态转移函数
- 状态文件 `~/.coding-agent/task_state.json` — 持久化当前 task 的 phase、completed_steps、next_step、known_issues
- 断点续传：`coding-agent --resume` 读取 `task_state.json` 恢复
- Agent 每轮执行前必须读 `task_state.json`，执行后必须更新

### P12-4: 真实语义记忆

- 替换当前 `numpy + TF-IDF` 词袋模型为本地 embedding
- 候选实现：`sentence-transformers`（`all-MiniLM-L6-v2`，90MB）或 `BGE-small-en-v1.5`（33MB）
- 保持 SQLite 存储 + numpy 向量索引，**只替换 embedding 提取**
- 增加 `L3 语义搜索` 工具：`semantic_search(query, k=10)` 返回真正语义相关而非词频相关
- 验收：搜索"如何处理并发"能找到包含"async/await"、"asyncio.gather"的记忆

---

## Phase 13: P1 Differentiation — 差异化竞争力 📋

> 详细规格：见 `docs/PR-05..PR-09` 五份 PR 文档
> 依据：[docs/1.md §5.2 上下文工程管道](../docs/1.md) | [docs/参考.md Aider repomap / MoAI-ADK codmap](../docs/参考.md)

- [ ] `AC-P13-1` repomap 注入（Aider codmap 风格）
- [ ] `AC-P13-2` SDD 解析器（Acceptance Criteria 提取）
- [ ] `AC-P13-3` Orchestrator PM Agent（编排-执行者模式）
- [ ] `AC-P13-4` 不可变审计日志
- [ ] `AC-P13-5` Evaluator Agent

### P13-1: repomap 注入（Aider codmap 风格）

- 借鉴 Aider 的 `repomap`：在 system prompt 注入**项目代码地图**（文件路径 + 大小 + 最近修改时间 + 顶层符号签名）
- 借鉴 MoAI-ADK 的 `codmap`：额外标注模块依赖关系
- 新增 `index/codmap.py` — 生成器
- 触发时机：每次 LLM call 前重新生成（如果 mtime 变化）
- 输出格式：
  ```
  src/auth/login.py (340 lines, mod 2d ago)
    class AuthService: ...
    def verify_token(token: str) -> bool: ...
  ```

### P13-2: SDD 解析器（Acceptance Criteria 提取）

- 升级 `agent/core/spec_loader.py`：从「解析 Phase 标题」升级到「提取 AC」
- 新格式支持：
  - `## Phase X` 标题
  - `### P-X-N: <name>` 子项
  - 验收 `- [ ] <criterion>` 自动提取为 `AcceptanceCriterion(id, description, status)`
- 工具链：`spec_status` 返回所有未完成 AC；`mark_ac_done(ac_id)`；`verify_against_spec(phase_id)`

### P13-3: Orchestrator PM Agent（编排-执行者模式）

- 实现 1.md §7.1 主 Agent（PM）模式
- 新增 `agent/agents/orchestrator.py` — `OrchestratorAgent` 负责任务分解、调度子 Agent、合并结果
- 子 Agent 集群（专业化）：
  - `CodeGenerator`（编码）
  - `TestEngineer`（写测试 + 验证）
  - `Reviewer`（架构合规性）
  - `DevOps`（环境/CI 交互）
- 通信协议：`TaskRequest` / `TaskResponse` dataclass，通过 EventBus（PR-01）传递
- 验收：复杂任务「实现带鉴权的 API」由 Orchestrator 自动拆分为 6+ 子任务，并行/串行混合执行

### P13-4: 不可变审计日志

- 借鉴 1.md §8 安全审计
- 新增 `agent/core/audit_log.py` — append-only JSONL
- 记录每条 agent 行为：`{ts, session_id, agent_id, action, tool, args_hash, result_hash, permission_decision}`
- 路径：`~/.coding-agent/audit/{date}.jsonl`（按天滚动）
- 不提供 delete API，只能 rotate（30 天后归档）
- 工具：`audit_query` 支持时间范围 + agent_id 过滤

### P13-5: Evaluator Agent

- 借鉴 1.md §9 评估器 Agent
- 新增 `agent/agents/evaluator.py` — 独立 LLM agent，对完成任务多维评分
- 评分维度：完成度（0-10）/ 代码质量（0-10）/ 安全性（0-10）/ 性能（0-10）
- 输出 `SCORE.md`：
  ```markdown
  # Task Evaluation
  - Task: 实现带鉴权的 API
  - Score: 8.2/10
  - 完成度: 9/10 — 所有 AC 满足
  - 代码质量: 8/10 — 测试覆盖 87%
  - 安全性: 7/10 — 缺 rate limiting
  - 性能: 9/10 — P95 < 100ms
  - 建议改进: 添加 rate limiting 中间件
  ```

---

## Phase 14: P2 Observability — 可观测与治理 📋

> 详细规格：见 `docs/PR-10..PR-13` 四份 PR 文档
> 依据：[docs/1.md §9 全链路可观测性 / §8 纵深防御 / §11 治理与持续进化 / §10 长时任务与断点续传](../docs/1.md)

- [ ] `AC-P14-1` OpenTelemetry 集成
- [ ] `AC-P14-2` Dual-agent 互审
- [ ] `AC-P14-3` AB Testing 框架
- [ ] `AC-P14-4` `claude-progress.txt` 进度锚点

### P14-1: OpenTelemetry 集成

- 实现 1.md §9 三大支柱
- 新增 `agent/observability/tracing.py` — OTel Tracer 包装
- Span 覆盖：每个 LLM call、每个 tool execution、每个 Hook execute
- Metrics：tool 调用次数 / 平均耗时 / 失败率 / token 消耗
- Logs：结构化 JSON log（与 audit log 区分：audit 是合规记录，otel log 是调试记录）
- 导出：本地 OTLP endpoint（`localhost:4317`）+ 控制台 fallback

### P14-2: Dual-agent 互审

- 实现 1.md §8 全生命周期权限中的「高风险操作需双 Agent 互审」
- 适用范围：CRITICAL 风险操作（写文件、Shell、Git push、PR 创建）
- 流程：主 Agent 提议 → 第二个独立 Agent 用不同 model 复审 → 一致则放行
- 引入 reviewer pool 概念：复审 Agent 来自不同 provider（OpenAI + Anthropic 互审）
- 失败时上报用户并附两个 Agent 的分歧分析

### P14-3: AB Testing 框架

- 实现 1.md §11 AB 测试
- 适用对象：技能 prompt 模板、system prompt 段、工具默认参数
- 新增 `agent/governance/ab_test.py` — 流量分配 + 结果聚合
- 流量切分：哈希 user_id → 桶 A / 桶 B
- 指标：task 成功率、token 效率、用户满意度
- 决策：胜出方自动全量上线，落败方归档到 `skills/.archive/`

### P14-4: `claude-progress.txt` 进度锚点

- 实现 1.md §10 进度锚点强制读写
- 路径：`WORKSPACE/.claude-progress.txt`（项目级，可 git 追踪）
- 格式：
  ```
  [current_task]: 实现带鉴权的 API
  [current_step]: 3/8 (writing login endpoint)
  [next_step]: 4/8 (write auth middleware test)
  [op_hash]: sha256:abc123...
  [known_issues]: - rate limiting 未实现
  [updated_at]: 2026-06-06T10:23:45
  ```
- 强制时机：每轮 LLM call 前自动 read，每轮 tool execution 后自动 write
- 断点续传：进程崩溃后，新会话读 progress.txt 立即恢复上下文

---

## 当前状态

```
已完成:   Phase 0–9 (基础建设 + P0 核心体验)
部分实现: Phase 10–12 (P1 提升 + P2 差异化 + P0 Foundation)
计划中:   Phase 13–14 (P1 Differentiation + P2 Observability)

测试: 1528 collected，当前 1519 passed / 1 failed / 6 skipped / 2 xfailed
      唯一失败：test_integration_all_tools — 测试用 `verify_acs(phase_id="P0")`，但 SPECS.md 阶段 ID 为 `P0-1` / `P0-2` / ...（id 解析问题待定；详见 PR-TEST-REPORT.md）
```
