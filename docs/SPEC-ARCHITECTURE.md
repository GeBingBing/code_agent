# SPEC: 架构层面升级

## 目标

对标 Claude Code 的架构能力，补齐 coding-agent 在编排、持久化、权限、恢复四个架构维度的差距。

---

## P1 — 后台任务 + 非阻塞子 Agent

### 现状

子 agent 通过 `spawn_sub_agent` / `spawn_parallel` 创建，父 agent **同步等待**完成。对话卡死，用户无法继续输入。

### 目标

```
用户: "写前端 + 后端 + 测试"
  │
  ├── spawn_parallel([ui, api, test])  → 3 个子 agent 后台运行
  │
  ├── 用户继续对话: "数据库用 PostgreSQL"
  │      → agent 修改 DB 模型，不影响正在跑的 3 个子任务
  │
  ├── [后台] ui agent 完成 → 通知 "前端已完成"
  ├── [后台] api agent 完成 → 通知 "API 已完成"
  └── [后台] test agent 完成 → 通知 "测试已完成"
```

### 改动

| 组件 | 改动 |
|------|------|
| `agent/tools/sub_agent.py` | `spawn_sub_agent` 返回 `task_id`，创建 `asyncio.create_task` 不 await |
| `agent/core/engine.py` | `run_stream` 支持 `background_task_completed` 事件，轮询已完成的后台任务 |
| `ui/cli.py` | `run()` 主循环支持接收后台任务通知，显示 "N background tasks running" |
| `agent/core/subagent_registry.py` | 增加 `list_background()` / `get_result(task_id)` / `cancel(task_id)` |

### 验收

- `spawn_sub_agent` 后不阻塞，立即返回 `task_id`
- 用户可继续发消息，后台任务正常完成
- `/tasks` 命令可查看后台任务状态

---

## P2 — 会话持久化与恢复

### 现状

对话状态（history、memory、plan）仅存内存，进程退出后全部丢失。Ctrl+C 后再打开是空白。

### 目标

```
coding-agent
> 写一个 REST API
  ... (大量对话和代码生成) ...
> Ctrl+C

$ coding-agent --resume
  Resuming session from 2026-06-03 14:32:01
  上次你写到了 /users 接口，继续吗？
```

### 改动

| 组件 | 改动 |
|------|------|
| `agent/core/session.py` | **新建** — `SessionManager` 负责 save/load/list |
| `agent/core/engine.py` | `__init__` 支持 `session_id` 参数，自动 resume |
| `agent/core/memory.py` | 增加 `serialize()` / `deserialize()` 方法 |
| `ui/cli.py` | `main()` 增加 `--resume` / `--list-sessions` 参数 |
| `~/.coding-agent/sessions/` | 持久化目录，JSON 格式存储 |

### 存储格式

```json
{
  "session_id": "20260603-143201-a3f2",
  "created_at": "2026-06-03T14:32:01",
  "updated_at": "2026-06-03T15:10:45",
  "task": "写一个 REST API",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]}
  ],
  "memory": {
    "long_term": "...",
    "file_context": ["src/api/users.py", "src/models/user.py"]
  },
  "plan": null
}
```

### 验收

- `coding-agent --resume` 恢复最近一次会话
- `coding-agent --list-sessions` 列出所有会话
- 恢复后 `/memory` 能看到之前的长期记忆

---

## P3 — Token 预算感知 + 自动 Compact

### 现状

`memory.py` 的 `_count_tokens` 是简单启发式（CJK=1，其他=1/3），`_compress` 只在 `len(working_memory) > 40` 时触发。不感知真实 context window 上限，不追踪 token 消耗。

### 目标

```
⠋ thinking... · 12s · ⬇842 · 35% context used
```

引擎实时追踪 token 消耗，接近阈值时自动 compact（摘要旧消息），显示上下文使用百分比。

### 改动

| 组件 | 改动 |
|------|------|
| `agent/core/engine.py` | `run_stream` 累积 `total_input_tokens`，每轮后检查阈值 |
| `agent/core/memory.py` | `_compress` → `compact()`：生成摘要 → 注入为系统消息 → 清除旧消息 |
| `agent/llm/client.py` | `chat()` 返回结构增加 `usage` 字段（从 API 响应提取） |
| `agent/core/config.py` | 增加 `context_window` / `compact_threshold` 配置项 |
| `agent/prompts/assembler.py` | 增加 compact 边界标记，告诉 LLM "以下是之前对话的摘要" |

### 阈值逻辑

```
context_window = 128000  (模型 context，可配置)
compact_threshold = 0.75  (达到 75% 时触发 compact)

if total_tokens > context_window * compact_threshold:
    summary = await generate_summary(old_messages)
    memory.compact(summary)
```

### 验收

- 长对话自动 compact，不超出 context window
- spinner 显示 `35% context used`
- `/context` 命令显示详细 token 分布

---

## P4 — 权限规则系统

### 现状

权限基于硬编码的 `RiskLevel`（LOW/MEDIUM/HIGH/CRITICAL），规则不可配置。用户无法自定义 "git 操作一律允许" 或 "永远禁止 rm"。

### 目标

```
# ~/.coding-agent/config.json
{
  "permissions": {
    "allow": ["Bash(git *)", "Bash(go *)", "Bash(python *)"],
    "deny": ["Bash(rm *)", "Bash(sudo *)", "Bash(curl *)"],
    "ask": ["Write(*)", "Bash(docker *)"]
  }
}
```

权限评估流程：

```
工具调用 → deny 规则检查 → allow 规则检查 → 风险等级 → 确认/拒绝
              │                  │
              └── 命中 → 拒绝     └── 命中 → 放行
```

### 改动

| 组件 | 改动 |
|------|------|
| `agent/core/permissions.py` | `PermissionManager` 增加规则引擎，`_match_rule(pattern, tool_call)` |
| `agent/core/config.py` | 增加 `permissions.allow/deny/ask` 配置项 |
| `~/.coding-agent/config.json` | 支持 `permissions` 字段 |
| `CODING_AGENT.md` | 支持 `@permissions allow Bash(git *)` 指令 |

### 规则语法

```
Bash(git *)        → 匹配 execute_command 且命令以 "git" 开头
Write(*)           → 匹配所有写操作
Write(src/**)       → 匹配路径在 src/ 下的写操作
mcp__server__*     → 匹配 MCP 工具
```

### 验收

- 配置 `allow: ["Bash(git *)"]` 后，`git status` / `git diff` 不再弹确认
- 配置 `deny: ["Bash(rm *)"]` 后，`rm -f file.txt` 直接拦截
- Shadow 检测：当低优先级规则被高优先级覆盖时给出警告

---

## P5 — 错误恢复策略

### 现状

工具失败后仅返回错误给 LLM，LLM 自行决定是否重试。没有自动的错误分类和恢复策略。

### 目标

```
工具失败 → 错误分类 → 自动恢复策略
  │
  ├── 包名错误 (pip install hermes 失败)
  │     → 自动尝试 hermes-agent / hermes-cli / hermes-sdk
  │
  ├── 网络超时
  │     → 等待 2s 重试，最多 3 次
  │
  ├── 文件不存在 (read_file "foo.py" 失败)
  │     → 自动搜索相似文件名 (fuzzy match)
  │
  └── 权限拒绝
        → 提示用户修改权限规则
```

### 改动

| 组件 | 改动 |
|------|------|
| `agent/core/error_recovery.py` | **新建** — `ErrorClassifier` + `RecoveryStrategy` |
| `agent/core/engine.py` | `_execute_tool` 失败后调用 `recovery.attempt(tool_name, args, error)` |
| `agent/tools/install.py` | 已有的智能后缀重试逻辑移到 recovery 层 |

### 验收

- `pip install hermes` 失败 → 自动尝试 `hermes-agent` → 成功
- `read_file("fo.py")` 失败 → 自动找到 `foo.py` → 成功
- 网络超时自动重试，不占用户步数

---

## P6 — Fork 对话分支

### 现状

一条线走到底。想尝试不同方案必须手动记录状态，重新开始。

### 目标

```
> 写一个登录功能

  [方案A: JWT]
  agent 生成了 jwt_auth.py

> /fork "用 Session 方案重新写"

  [方案B: Session] ← 新分支，有完整上下文
  agent 生成了 session_auth.py

> /switch main
  切回 JWT 方案继续
```

### 改动

| 组件 | 改动 |
|------|------|
| `agent/core/session.py` | 增加 `fork(from_session, label)` 方法 |
| `ui/cli.py` | 增加 `/fork [label]` / `/switch [id]` 命令 |
| `agent/core/engine.py` | Engine 构造时接受 session 数据 |

### 验收

- `/fork "尝试 React"` 创建新分支，包含当前上下文
- `/switch` 在分支间切换
- 每个分支独立 memory，不互相污染

---

## 优先级排序

| 优先级 | 项目 | 工作量 | 影响 |
|--------|------|--------|------|
| **P1** | 后台任务 + 非阻塞子 Agent | 3-4天 | 大 — 消除对话卡死 |
| **P2** | 会话持久化与恢复 | 2-3天 | 大 — 不再丢失工作 |
| **P3** | Token 预算 + 自动 Compact | 2-3天 | 中 — 长对话不爆 context |
| **P4** | 权限规则系统 | 2-3天 | 中 — 减少重复确认 |
| **P5** | 错误恢复策略 | 2天 | 中 — 减少无意义失败 |
| **P6** | Fork 对话分支 | 1-2天 | 小 — 探索不同方案 |

---

## 不做

- **多输出管线**（print/structuredIO）— 当前仅 CLI，不需要
- **React/Ink 终端 UI** — 太重，prompt_toolkit 足够
- **完整 hook/插件系统** — 先做好核心，插件以后再说
- **MCP 市场集成** — 当前手动配置够用
