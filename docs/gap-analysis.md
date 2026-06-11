# Gap Analysis: 距离目标还有多远

> 目标：类 Claude Code 的 code agent，融合 specDD / TDD / Harness / OpenClaw / Hermes 精华

---

## 完成度总览

```
████████░░░░░░░░░░░░ ████░░░░░░░░░░░░░░░░ ░░░░░░░░░░░░░░░░░░░░ ██████░░░░░░░░░░░░░░
   Core Agent (70%)      specDD (20%)         TDD (5%)        Harness (0%)
████░░░░░░░░░░░░░░░░ ░░░░░░░░░░░░░░░░░░░░ ████████░░░░░░░░░░░░
 OpenClaw (30%)        Hermes (5%)        Claude Code UX (40%)
```

**总体估算：约 30-35% 到达目标。**

---

## 一、Core Agent（已有基础 ≈70%）

| 能力 | 状态 | 差距 |
|------|------|------|
| ReAct 循环 | ✅ 已实现 | — |
| LLM 多 provider | ✅ 6 家 | provider 检测有 bug（已修） |
| 文件读写/编辑 | ✅ 完整 | diff 编辑可 diff-match-patch、hunk 级别预览 |
| Shell 执行 | ⚠️ 有但不够安全 | 需 migrate 到 create_subprocess_exec；缺 cwd 校验 |
| Git 操作 | ⚠️ 基础 | 缺 branch 管理、PR 创建、commit message 生成 |
| 权限系统 | ✅ 4 模式 | CRITICAL 检查顺序已修 |
| 三层记忆 | ⚠️ 实现有 bug | compress 配对已修；向量记忆形同虚设（hash 随机化已修，但仍是词袋模型非语义搜索） |
| 流式输出 | ✅ 支持 | typewriter 用 time.sleep（已修） |
| 代码索引 | ⚠️ AST 仅 Python | 缺 tree-sitter，不支持跨文件引用分析 |
| 子 Agent | ⚠️ 可 spawn | 缺 agent 间通信、结果合并、任务拆分策略 |
| 沙箱 | ⚠️ 有 Docker | docker check 有 bug（已修）；缺 commit 变更到宿主 |

---

## 二、specDD（几乎没有 ≈20%）

specDD 的核心理念是**先写规格说明，再从规格生成代码，最后验证实现是否符合规格**。

| 能力 | 状态 | 差距 |
|------|------|------|
| SPECS.md 解析 | ❌ 不存在 | SPECS.md 是给人看的文档，Agent 无法理解项目 spec |
| 从 SPEC 生成任务拆解 | ❌ 不存在 | 没有 "phase 分解为 task list" 的流程 |
| Spec → Code 生成 | ❌ 不存在 | 没有根据 spec 描述自动生成骨架代码 |
| Spec 验证 | ❌ 不存在 | 实现完后不会对比 spec 检查是否遗漏 |
| Phase 追踪 | ❌ 不存在 | Agent 不知道自己当前在哪个 phase、完成了什么 |
| Spec 变更检测 | ❌ 不存在 | SPEC 改了代码不会自动同步 |
| E2E 规格驱动测试 | ❌ 不存在 | 没有从 spec 场景生成集成测试 |

**需要补充的核心能力：**
- `agent/core/spec_engine.py` — 解析 SPECS.md，提取 feature、phase、acceptance criteria
- 任务规划器：将 spec 分解为可执行 task，跟踪完成状态
- 规格验证器：对比实现与 spec，标记 gap
- Spec 感知的 system prompt：告诉 Agent 当前 project 的 spec 上下文

---

## 三、TDD（几乎没有 ≈5%）

TDD 精髓是 **Red → Green → Refactor** 循环。当前项目有测试文件，但 Agent 完全不参与测试流程。

| 能力 | 状态 | 差距 |
|------|------|------|
| 写测试 | ❌ 不存在 | Agent 不会先写测试再写代码 |
| 运行测试 | ❌ 未集成 | 没有 "run tests" 作为内置步骤 |
| 失败定位 | ❌ 不存在 | 没有解析 pytest 输出、定位失败原因 |
| 自动修复 | ❌ 不存在 | 没有 "test failed → analyze → fix → re-run" 循环 |
| 覆盖率分析 | ❌ 不存在 | 没有 `pytest --cov` 集成 |
| 回归保护 | ⚠️ 间接 | 手动可以运行 pytest，但 Agent 从不主动做 |
| 快/慢测试分层 | ❌ 不存在 | 没有区分 unit / integration / e2e |
| Mutation testing | ❌ 不存在 | — |

**需要补充的核心能力：**
- `agent/core/tdd_loop.py` — Red → Green → Refactor 状态机
- 测试运行器工具：`run_tests(path, marker)` 工具
- 测试失败分析器：解析 traceback，关联到具体代码行
- 自动修复策略：重试 → 回退 → 二分定位

---

## 四、Harness（完全没有 ≈0%）

Harness 在工程实践中指 **CI/CD pipeline、部署编排、feature flag、金丝雀发布、可观测性**。

| 能力 | 状态 | 差距 |
|------|------|------|
| CI pipeline 集成 | ❌ 不存在 | 不生成 GitHub Actions / GitLab CI |
| 构建系统 | ❌ 不存在 | 不管理 build step |
| Docker 构建 | ❌ 不存在 | 不生成 Dockerfile 或 docker-compose |
| 部署 | ❌ 不存在 | 不处理 deploy staging/prod |
| Feature flag | ❌ 不存在 | 没有 flag 系统 |
| 金丝雀发布 | ❌ 不存在 | — |
| 健康检查 | ❌ 不存在 | 不生成 health check endpoint |
| 日志/监控 | ❌ 不存在 | 不集成 logging / metrics |
| 告警规则 | ❌ 不存在 | — |
| 环境管理 | ❌ 不存在 | 不管理 dev/staging/prod 环境差异 |
| Secret 管理 | ❌ 不存在 | 不处理 .env / vault |

这个维度完全空白。是否纳入取决于你的产品定位：
- 如果是**本地 coding agent**（Claude Code 定位），Harness 不在范围内
- 如果是**全流程 DevOps agent**，那需要大量建设

---

## 五、OpenClaw（子 Agent 系统 ≈30%）

OpenClaw 强调 **多 Agent 树形组织、生命周期管理、Agent 间通信**。

| 能力 | 状态 | 差距 |
|------|------|------|
| Agent 树形注册 | ✅ SubAgentRegistry | cleanup 孤立引用已修 |
| 深度限制 | ✅ MAX_DEPTH=5 | — |
| Spawn/Kill | ✅ 已实现 | kill 后状态覆盖已修 |
| 生命周期 | ✅ RUNNING→COMPLETED/FAILED/KILLED | — |
| 结果回传 | ⚠️ 简单 string 返回 | 缺结构化结果（exit code、output、files changed） |
| Agent 间通信 | ❌ 不存在 | 子 agent 无法向父 agent 发送消息 |
| 任务拆分策略 | ❌ 不存在 | 没有智能拆分（"这个 task 应该拆成几个子任务"） |
| Agent 专业化 | ❌ 不存在 | 所有 sub-agent 用相同 config，无角色区分 |
| 并行 Agent | ❌ 不存在 | spawn 都是串行等待，不能并行执行独立子任务 |
| 结果合并 | ❌ 不存在 | 多个子 agent 返回后不做结果归纳 |
| 资源隔离 | ⚠️ Docker sandbox | 但子 agent 之间共享 workspace |
| Agent 限额 | ❌ 不存在 | 无并发数限制、无 token 预算分配 |

---

## 六、Hermes（结构化推理 ≈5%）

Hermes 强调 **结构化思维、工具选择推理、chain-of-thought**。

| 能力 | 状态 | 差距 |
|------|------|------|
| 显式思考链 | ❌ 没有 | 没有 `think` phase，Agent 直接行动 |
| 工具选择推理 | ❌ 不存在 | 不解释为什么选某个 tool |
| 结构化输出 | ❌ 不存在 | 所有输出都是自由文本 |
| 计划生成 | ❌ 不存在 | 没有 "先分析需求，输出执行计划，再执行" |
| 计划验证 | ❌ 不存在 | 没有 "执行完检查计划是否完成" |
| 自我纠错 | ⚠️ 基本重试 | retry 只是盲目重跑，不做根因分析 |
| 反思机制 | ❌ 不存在 | 不回顾之前的错误来改进 |
| 上下文压缩策略 | ⚠️ 基本 | compress 只看消息数，不看语义重要性 |

**需要补充的核心能力：**
- Plan 阶段：先输出结构化执行计划（JSON），再逐步执行
- Think 标签：在 tool call 前生成推理过程
- 工具选择 reasoning：给 LLM 更多上下文来选对工具
- 反思回调：每个 task 结束后评估质量

---

## 七、Claude Code UX 对标（≈40%）

| Claude Code 特性 | 你的项目 | 差距 |
|---|---|---|
| 流式输出 | ✅ 有 | — |
| 彩色 diff | ⚠️ 有 ANSI 色 | 缺 side-by-side diff 视图 |
| Tool call 展示 | ✅ 有 | — |
| Plan 模式 | ✅ 有 | 但 plan 只做只读，不做结构化计划生成 |
| Permission "Always allow" | ✅ 有缓存 | key 只认 path/command，不校验 content hash |
| /command 系统 | ❌ 没有 | /commit, /review-pr, /clear 等 slash commands |
| CLAUDE.md 项目指令 | ❌ 没有 | Agent 不读取项目级 CLAUDE.md |
| MEMORY.md 记忆 | ⚠️ L3 记忆 | 格式不同（key-value 而非语义段落） |
| .claude/ 配置目录 | ❌ 没有 | 缺 settings.json、hooks 系统 |
| VS Code 扩展 | ⚠️ 有 | CORS 修复后应可用，但缺 inline edit、terminal 集成 |
| IDE 内联补全 | ⚠️ 框架存在 | 未端到端验证 |
| 多轮对话记忆 | ⚠️ 已加基本支持 | CLI 注入上下文，但非原生 engine 支持 |
| Task 进度条 | ⚠️ 有 step 计数 | 缺 rich/progress bar |
| Undo/回退 | ❌ 没有 | 沙箱有 snapshot 但未集成到 undo workflow |
| Background task | ❌ 没有 | — |
| Conversation fork | ❌ 没有 | — |

---

## 八、按优先级排列的 Roadmap

### 🔴 P0：立即补齐 — 阻碍日常可用性 ✅ 已完成

1. ✅ **CODING_AGENT.md 项目指令加载** — Engine 启动时读取 workspace 根目录 CODING_AGENT.md，注入 system prompt
2. ✅ **Plan-then-execute 模式** — run_plan() + run_execute() 双阶段 API，ExecutionPlan 数据结构，CLI 确认交互
3. ✅ **TDD 引导** — run_tests 工具 + system prompt 注入 TDD suggestion
4. ✅ **Slash commands** — 11 个命令：/clear /plan /commit /help /model /mode /memory /status /context /review /undo
5. ✅ **Undo/回退** — /undo 命令（changes / commit）

> 实现参考：`docs/SPEC-P0.md`

### 🟡 P1：显著提升能力

6. **specDD 集成** — SPECS.md 解析 + phase 追踪
7. **子 Agent 并行执行** — 独立子任务并行跑
8. **tree-sitter 代码索引** — 替代 AST + regex 混合方案
9. **Textual TUI** — 替代原始 ANSI CLI
10. **更好的语境管理** — 语义压缩、token 预算分配

### 🟢 P2：差异化竞争力

11. **Agent 自我进化** — 将成功任务转化为 skills
12. **多文件重构感知** — 跨文件引用分析
13. **Git 深度集成** — commit message 生成、PR 创建、branch 策略
14. **API/MCP 工具扩展** — 集成外部服务

### ⚫ Phase N：按需建设

15. **Harness CI/CD** — 如果走全流程 DevOps agent 路线
16. **多模态** — 截图理解、UI 测试
17. **Code review agent** — 专门 review PR 的子 agent

---

## 结论

当前项目是一个**基础扎实的 ReAct coding agent**，骨架完整但肌肉不足。有清晰的模块划分、工具生态、权限和记忆系统，修复了 31 个 bug 后稳定性显著提升。

但要达到 "融合 Claude Code + specDD + TDD + OpenClaw + Hermes" 的愿景，需要在以下方面有本质性突破：

- **specDD**：几乎从零建设（spec 解析、验证、追踪）
- **TDD**：几乎从零建设（测试闭环）
- **结构化推理**：引入 plan-then-execute + think chain
- **工程化体验**：CLAUDE.md、slash commands、TUI
- **子 Agent 协同**：从串行 spawn 升级到并行协作
