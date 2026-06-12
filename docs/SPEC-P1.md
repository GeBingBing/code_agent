# SPEC-P1: 显著提升能力

> 关联：[gap-analysis.md](./gap-analysis.md) | 状态：已完成

---

## P1-1: 更好的语境管理

### 问题

当前 `_compress()` 只按消息数裁剪（保留最近 6 条），不管内容重要性。导致：
- 关键上下文（如 system prompt 的核心指令）可能被压缩掉
- 不重要的大段输出（如 read_file 的 500 行结果）占满保留区
- Token 估算用 `len//3` 对中文仍不精确

### 设计

**语义压缩策略**（替代简单截断）：

```
优先级排序：
  1. system 消息 → 始终保留（不可压缩）
  2. 最近的 tool_call/tool_result 对 → 成对保留
  3. 其余消息按 token 预算裁剪

压缩方式：
  - read_file 大结果 → 只保留前 20 行 + "... (N lines truncated)"
  - execute_command 输出 → 只保留前 200 字符 + "... (N chars truncated)"
  - 其他 → 正常保留
```

**Token 估算改进**：用 tiktoken（如果可用）替代 `len//3` 启发式，否则用 CJK 感知的估算。

### 改动文件

- `agent/core/memory.py` — `_compress()` 重写 + `_estimate_tokens()` 改进

### 验收

- [ ] system 消息不被压缩
- [ ] 大文件读取结果自动截断摘要
- [ ] token 估算误差 < 30%

---

## P1-2: specDD 集成

### 问题

SPECS.md 存在但 Agent 完全不知道。Agent 不知道自己当前在实现哪个 phase。

### 设计

**SPECS.md 解析器**：

```
Engine.__init__()
  → 检测 WORKSPACE/SPECS.md
  → 解析 phase 结构（## Phase X: ...）
  → 提取当前 active phase
  → 注入 system prompt：[Spec context] 段
```

**解析格式**：正则匹配 `## Phase \d+: <名称>` + `✅`/`⚠️`/`🔜` 状态标记。

### 改动文件

- `agent/core/spec_loader.py` — **新** — SPECS.md 解析
- `agent/core/engine.py` — `_load_spec_context()`
- `agent/prompts/assembler.py` — spec_context 参数

### 验收

- [ ] 有 SPECS.md 时 system prompt 包含 spec context
- [ ] `/status` 显示当前 spec phase

---

## P1-3: 子 Agent 并行执行

### 问题

`spawn_sub_agent` 串行等待，无法并行跑独立子任务。

### 设计

新增 `spawn_parallel` 模式：

```python
async def spawn_parallel(tasks: List[SubTask]) -> List[Result]:
    tasks_coros = [spawn_and_wait(t) for t in tasks]
    return await asyncio.gather(*tasks_coros)
```

不改现有 API，新增 `parallel=True` 参数。

### 改动文件

- `agent/tools/sub_agent.py` — 并行 spawn 支持
- `agent/core/subagent_registry.py` — 并发安全

### 验收

- [ ] 多个子 Agent 可以并行运行
- [ ] 结果按任务顺序返回
- [ ] 深度限制和 kill 在并行场景正常工作

---

## P1-4: tree-sitter 代码索引

### 问题

当前用 Python AST + regex，不支持精确的跨语言解析。

### 设计

引入 tree-sitter：
- Python 已有 AST，先加 JS/TS/Go/Rust/Java 的高质量解析
- 缓存到 JSON（mtime 增量更新）

### 改动文件

- `index/code_indexer.py` — 集成 tree-sitter
- `requirements.txt` — tree-sitter + tree-sitter-* 依赖

### 验收

- [ ] tree-sitter 解析 JS/TS/Go/Rust/Java
- [ ] 性能不低于当前 regex 方案

---

## P1-5: Textual TUI

### 问题

当前原始 ANSI CLI 功能齐全但视觉效果朴素。

### 设计

用 Textual 框架重写 CLI：
- 左侧：文件树 / 对话历史
- 中间：流式输出
- 底部：输入栏
- 快捷键保留（方向键、Ctrl 组合）

### 改动文件

- `ui/tui.py` — **新** — Textual 实现
- `pyproject.toml` — textual 保留为可选 extra（`tui = ["textual>=0.50.0"]`），通过 `pip install -e .[tui]` 安装

### 验收

- [x] Textual TUI 可正常启动和交互
- [x] 保留所有快捷键
- [x] 原 CLI 保留（`--cli` 参数）

---

## 实施顺序

```
P1-1: 语境管理     (1 文件，小)
P1-2: specDD 集成   (3 文件，中)
P1-3: 子 Agent 并行 (2 文件，中)
P1-4: tree-sitter   (2 文件，中)
P1-5: Textual TUI   (1 新文件，大)
```
