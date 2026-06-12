# PR-01: EventBus + Hook 系统

> 关联：SPECS.md Phase 12-1 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §3.1 事件驱动的插件化核心循环](../1.md) | [docs/参考.md 工程骨架 codex-agent-framework](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Event bus 实现 | `asyncio.Queue` + pub/sub | 与现有 asyncio 架构融合，零依赖 |
| Hook 同步性 | 异步优先 + 同步回退 | LLM call 是 async，UI 更新可同步 |
| Hook 失败处理 | 中断 + 记录 | 安全关键 Hook（permission check）失败必须阻止 |
| 重构范围 | 仅 `engine.run_stream` | 最小爆炸半径，不动 plan / memory / tools |
| 向后兼容 | 保留旧 `run()` 入口 | 旧调用方不需要改 |

---

## 现状 / 目标

**现状**（`agent/core/engine.py:run_stream`）：
- 直接调用 `llm.chat()` → 解析 tool_calls → 调 `_execute_tool` → 拼接到 messages
- 流式输出靠硬编码的 `time.sleep` 延迟（已修过，但仍是命令式而非声明式）
- 业务逻辑（token 估算、permission 检查、audit 记录）散落在 `if/else` 分支里
- 任何新能力（日志、监控、自定义拦截）都要改 engine.py

**目标**（1.md §3.1）：
```python
async def run(self, task):
    await self.hooks.execute("before_perceive", task)
    context = await self.perceive(task)
    await self.hooks.execute("before_decide", context)
    decision = await self.decide(context)
    await self.hooks.execute("before_act", decision)
    result = await self.act(decision)
    await self.hooks.execute("after_act", result)
    return result
```

- **观察者模式**：UI、日志、监控通过订阅事件工作，不直接调 engine
- **Hook 系统**：所有关键生命周期节点预留 Hook（`before_llm_call` 等 11 个），外挂逻辑永不侵入核心
- **零业务逻辑**：engine 自身不实现 token 计算、permission、audit——全部由 Hook 承担

---

## 设计

### EventBus

```python
# agent/core/event_bus.py (新文件)

@dataclass
class Event:
    type: str
    payload: dict
    ts: float = field(default_factory=time.time)

class EventBus:
    """Async pub/sub. Subscribers receive events via asyncio.Queue."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, event_type: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[event_type].append(q)
        return q

    async def emit(self, event_type: str, payload: dict) -> None:
        for q in self._subscribers.get(event_type, []):
            await q.put(Event(type=event_type, payload=payload))
        # Wildcard subscribers
        for q in self._subscribers.get("*", []):
            await q.put(Event(type=event_type, payload=payload))

    def stats(self) -> dict:
        return {k: len(v) for k, v in self._subscribers.items()}
```

### Hook Registry

```python
# agent/core/hooks.py (新文件)

class HookRegistry:
    """Plugin-style hooks. Hooks can be sync or async; can short-circuit by raising."""

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = defaultdict(list)

    def register(self, name: str, fn: Callable) -> None:
        """Register a hook. Hooks fire in registration order."""
        self._hooks[name].append(fn)

    def unregister(self, name: str, fn: Callable) -> None:
        self._hooks[name].remove(fn)

    async def execute(self, name: str, payload: Any) -> Any:
        """Run all hooks for `name`. If any raises, propagate (caller decides)."""
        for fn in self._hooks.get(name, []):
            result = fn(payload) if not asyncio.iscoroutinefunction(fn) \
                     else await fn(payload)
            if result is not None:
                payload = result  # Hooks can transform payload
        return payload

    def names(self) -> list[str]:
        return list(self._hooks.keys())
```

### 11 个标准 Hook 点

| Hook | 时机 | Payload | 用途 |
|------|------|---------|------|
| `before_perceive` | 任务开始 | `task: str` | 加载 project context、CODING_AGENT.md |
| `before_llm_call` | 调 LLM 前 | `messages: list` | 注入 repomap、压缩 message |
| `after_llm_call` | 调 LLM 后 | `response` | token 计数、记录 usage |
| `before_decide` | 解析 tool_call 前 | `response` | 拦截恶意 tool_call |
| `before_tool_execution` | 调 tool 前 | `tool, args` | permission check、audit log |
| `after_tool_execution` | 调 tool 后 | `result` | 格式化、截断大输出 |
| `on_error` | 异常发生 | `exception, context` | 错误恢复、重试策略 |
| `on_token` | 流式 token  | `chunk: str` | UI typewriter、tiktoken 估算 |
| `before_compact` | 压缩前 | `messages` | 摘要生成 |
| `after_compact` | 压缩后 | `summary` | 更新 memory |
| `on_session_end` | 会话结束 | `final_state` | 持久化、清理 |

### Engine 重构

`run_stream` 从：
```python
async for chunk in llm.chat_stream(messages):
    yield chunk
    # ... 业务逻辑
```

改为：
```python
async for chunk in llm.chat_stream(messages):
    await self.hooks.execute("on_token", chunk)
    await self.event_bus.emit("token", {"chunk": chunk})
    yield chunk
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/event_bus.py` | **新建** — EventBus（pub/sub） |
| `agent/core/hooks.py` | **新建** — HookRegistry + 11 个标准 hook 名常量 |
| `agent/core/engine.py` | `__init__` 增加 `self.event_bus = EventBus()` 和 `self.hooks = HookRegistry()`；`run_stream` 在 11 个关键点插入 `await self.hooks.execute(...)` 和 `await self.event_bus.emit(...)` |
| `agent/core/__init__.py` | export EventBus、HookRegistry |
| `tests/test_event_bus.py` | **新建** — pub/sub 基础、wildcard 订阅、stats |
| `tests/test_hooks.py` | **新建** — 注册/注销/执行顺序、同步/异步、payload 变换、异常传播 |
| `tests/test_engine_hooks.py` | **新建** — engine 在 11 个 hook 点都正确触发 |

---

## 验收标准

- [ ] `EventBus.subscribe("token")` 收到 `emit("token", {...})` 的所有事件
- [ ] `EventBus.subscribe("*")` 收到所有事件
- [ ] `HookRegistry.register("before_llm_call", fn)` 注册成功，`execute("before_llm_call", payload)` 顺序触发
- [ ] Hook 可同步或异步（`isinstance(fn, ...)` 区分）
- [ ] Hook 返回非 None 时，payload 被替换
- [ ] Hook 抛异常时，`execute` 重新抛出（caller 决定中断或忽略）
- [ ] engine.run_stream 在 11 个 hook 点都触发
- [ ] 旧 `run()` / `run_plan()` / `run_execute()` 入口仍工作
- [ ] 现有 398+ 测试不回归
- [ ] 手动验证：UI typewriter、token 计数、audit log 通过 hook 注入，**不需改 engine.py**

---

## 实施顺序

```
Step 1: agent/core/event_bus.py        (新文件，1h)
Step 2: agent/core/hooks.py            (新文件，1h)
Step 3: tests/test_event_bus.py        (新文件，0.5h)
Step 4: tests/test_hooks.py            (新文件，0.5h)
Step 5: agent/core/engine.py 改造       (改文件，2h)
Step 6: tests/test_engine_hooks.py     (新文件，1h)
Step 7: 跑 pytest tests/ 验证          (0.5h)
```

总工作量：~6.5h

---

## 与其他 PR 的关系

- **PR-02 TDD 状态机** / **PR-03 任务状态机** 通过 `on_error` / `before_act` hook 接入
- **PR-08 审计日志** 通过 `before_tool_execution` hook 自动记录
- **PR-10 OpenTelemetry** 通过 `before_llm_call` / `after_tool_execution` hook 发送 span
- **PR-13 进度锚点** 通过 `after_tool_execution` hook 写 progress.txt
- **本 PR 是基础设施**，先于其他 PR 实施

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/core/event_bus.py` | `EventBus.emit()` / `EventBus.on()` — 简单 pub/sub（`asyncio.Queue`） |
| `agent/core/hooks.py` | 11 个标准 hook 点：`before_perceive` / `before_llm_call` / `after_llm_call` / `before_decide` / `before_tool_execution` / `after_tool_execution` / `on_error` / `on_token` / `before_compact` / `after_compact` / `on_session_end` |
| `agent/core/engine.py` | `run_stream` 重构为事件驱动：`emit("before_llm_call") → llm.chat() → emit("after_llm_call") → ...` |
