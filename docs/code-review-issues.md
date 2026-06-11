# Code Agent 问题诊断报告

> 审查日期：2026-05-22
> 审查范围：全量代码（agent/core、agent/tools、agent/llm、agent/prompts、ui、index、server）

## Context

对 coding-agent 仓库进行全量代码审查，目标是从正确性、安全性、健壮性、性能和设计几个维度识别所有存在的问题。审查覆盖了所有核心模块（engine, memory, permissions, tools, llm, prompts, cli, index, server）。

---

## 严重 Bug（会导致程序崩溃或数据损坏）

### 1. `vector_memory.py:46` - Python `hash()` 随机化导致跨会话向量搜索完全失效

```python
h1 = hash(word) % dim  # Python 3.3+ 默认 PYTHONHASHSEED=random
```

每次启动进程时 Python 的 `hash()` 种子都不同，存储时的向量和查询时的向量在不同进程里算出来完全不一样，余弦相似度是随机值。所有跨进程/跨重启的向量记忆搜索功能彻底无效。应改用 `hashlib.sha256`。

### 2. `sandbox.py:27-28` - `_check_docker()` 返回值被丢弃，总是返回 True

```python
proc.returncode == 0  # 比较结果没赋值，是空操作
return True           # 不管 Docker 在不在都返回 True
```

这导致 `has_docker=True` 始终成立，后续实际调用 Docker 时才报错，且永远不会走到不带 Docker 的回退路径。

### 3. `engine.py:563` - `run()` 方法在 mock 模式下直接崩溃

`run_stream()` 在 line 351 检查了 `if self.llm is None: yield error; return`，但 `run()` 方法 line 563 直接调用 `self.llm.chat()` 没有 None 检查，mock 模式下 `self.llm` 被设为 `None`（line 292），导致 `AttributeError`。

### 4. `index/code_indexer.py:163` → `retriever.py:124` - 索引持久化后搜索崩溃

`load()` 保存索引时丢弃了源代码行（`lines=[]` 注释写"不持久化以节省空间"），但 `retriever.py._get_snippet()` 访问 `file_idx.lines[i]` 会抛出 IndexError。重启后所有代码搜索功能崩溃。

### 5. `index/code_indexer.py:102` - `ast.Node.parent` 属性不存在

`getattr(node, "parent", None)` 返回值永远是 `None`，Python标准库 `ast` 模块不会给节点设置 `parent` 属性。导致所有函数（包括嵌套函数）都被错误地判定为顶层函数。

### 6. `memory.py:76-91` - `_compress()` 破坏 tool_call/tool_result 配对

压缩旧消息为摘要时，如果某个 assistant 的 `tool_calls` 消息和对应的 `tool_result` 消息被分开（一个被压缩、一个留在最近 6 条中），LLM 会收到孤立的 tool_call 或孤立的 tool_result，造成上下文混乱。

---

## 高危安全问题

### 7. `shell.py:113-120` - timeout 完全无效

`asyncio.wait_for` 包裹的是 `create_subprocess_shell`（这个协程在进程启动后就返回了，毫秒级完成），不是 `communicate()`（真正等待进程结束的方法）。进程执行实际上没有超时限制，可以无限运行。

### 8. `shell.py` / `git_tool.py` / `sandbox.py` - 命令注入风险

三者都使用 `asyncio.create_subprocess_shell(cmd_string)` 直接拼接命令字符串：

- `shell.py`: `ls && rm -rf /` 通过第一个词的白名单检查后，`&&` 后面的恶意命令照样执行
- `git_tool.py`: 同样用 shell 方式执行，`status; rm -rf /` 可绕过子命令白名单
- `sandbox.py:59`: `f"docker run ... sh -c {command}"` 直接将用户输入注入容器 shell

应该改用 `create_subprocess_exec` + 参数列表，而不是字符串拼接进 shell。

### 9. `permissions.py:162-163` - BYPASS 模式下 CRITICAL 操作也被放行

Bypass 模式在 line 162 直接 `return True`，跳过了 line 166 的 `CRITICAL` 检查。即使在完全自动模式下，`sudo rm -rf /` 这类操作也应该被拦截。

### 10. `permissions.py:133-134` - 路径规则使用子串匹配

`if rule_pattern in path` 意味着 `/etc` 规则会匹配 `/path/to/etc_myconfig`，`workspace/` 会匹配任意包含该子串的路径。应该用 `Path` 解析或前缀匹配。

### 11. `web_fetch.py:28` - SSRF 漏洞，无 URL 校验

URL 直接传给 httpx 没有任何验证，攻击者可以：

- 访问云元数据服务：`http://169.254.169.254/latest/meta-data/`
- 扫描内网：`http://192.168.x.x`、`http://10.x.x.x`
- 访问 localhost：`http://127.0.0.1:6379/`

应该拦截内网 IP、localhost 和云元数据地址。

### 12. `engine.py:499-500` - 路径穿越风险

从文件写入路径的第一段推导 `current_project_dir`，如果 LLM 输出 `../evil/myfile`，project_dir 变成 `..`，后续 shell 命令的 cwd 就逃逸出 workspace。

### 13. `grep.py:40` - 绝对路径绕过根目录限制

`Path(root_dir) / "/etc"` 结果就是 `/etc`（绝对右操作数覆盖左操作数），可以读取任意系统文件。

---

## 中等问题

### 14. `plugin_hooks.py:106-112` - `call_post_tool()` 变量遮蔽导致返回类型错误

`result` 参数被循环内的局部变量 `result` 遮蔽，如果最后一个 hook 返回 `None` 或 `HookContext`，函数会返回 HookContext 对象而非工具结果，导致调用方拿到错误类型的值。整个插件系统也未集成到 engine 中。

### 15. `subagent_registry.py:280-298` - `cleanup_completed()` 在父记录的 `_children` 中留下孤立引用

只删除了 `_records` 中的条目，但没有从父记录的 `_children` 列表中移除引用。

### 16. `sub_agent.py:86-90` - 子代理错误被标记为成功

无论子代理执行成功还是失败，`run_and_complete` 的返回值都进入 `ToolResult(success=True, ...)` 路径。

### 17. `memory.py:107-134` - `remember()` 的键更新可能在截断时丢失

先原地更新已有键的值，再用 `[-50:]` 截断。如果更新的键在列表前部，截断会丢掉刚更新的条目。

### 18. `cli.py:142-143` - 多轮对话状态未传递给 AgentEngine

CLI 的 `self.history` 记录了对话历史，但每次任务都创建空白的 AgentEngine，多轮对话能力完全无效。

### 19. `server.py:32` - CORS 通配符写法无效

`http://localhost:*` 不是合法的 CORS origin 模式，被当作字面字符串匹配，VS Code 扩展的跨域请求可能被拒绝。

### 20. `server.py:85` - `asyncio.sleep(0.01)` 人为增加延迟

每个 SSE 事件之间人为插入 10ms 延迟，100+ 事件的任务增加 1+ 秒不必要的延迟。

### 21. `llm/client.py:112-137` - Provider 自动检测误判

模型名包含 `llama` 时优先匹配到 dashscope 而非 ollama，但 `/` 分隔符强烈暗示 ollama。

### 22. `demo.py` - DemoEngine 重复实现了整个 ReAct 循环

与真实 engine 的 `run()` 几乎完全重复。在真实 engine 修复的 bug 不会同步到 demo，反之亦然。

---

## 性能问题

### 23. `vector_memory.py:147` - O(N) 暴力搜索

每次搜索加载全部 embedding 到内存做余弦相似度计算，无索引、无近似、无早停。1万条记忆需要~10MB和~130万次浮点运算。

### 24. `index/retriever.py:26-96` - O(N) 暴力搜索

全量遍历所有文件、所有符号、所有行做子串匹配，无倒排索引、无 BM25/TF-IDF。

### 25. `memory.py:74` / `llm/client.py:192` - Token 估算严重不准

`len(text) // 4` 对中文是灾难性的（中文字符每个 1-3 token，而非 0.25），可能导致上下文窗口溢出。

### 26. `engine.py:219-231` - 同步 `time.sleep()` 在异步上下文中阻塞事件循环

### 27. `engine.py` - `run()` 和 `run_stream()` 大量重复代码

约 70 行工具执行/权限检查/记忆更新的逻辑在两个方法中完全重复。

---

## 设计/架构问题

### 28. SPECS.md 标记全部完成但与实际代码不一致

- Phase 6 标注完成但 tree-sitter 和语义搜索未实现
- Phase 8 标注完成但 Textual TUI 未实现（只用原始 ANSI 码）
- CLAUDE.md 声称用 SQLite 但代码中未见

### 29. 三层 `.env` 加载机制（client.py、server.py、cli.py 各自加载）

不一致的环境变量状态，且 client.py 自定义的 loader 不支持引号、多行值、转义，同时 `python-dotenv` 已经是依赖。

### 30. `sandbox.py:22` - `asyncio.run()` 不能在已运行的事件循环中调用

如果在测试/Jupyter/动态实例化中调用会抛出 RuntimeError。

### 31. 部分工具 schema 输出格式不一致

有的返回 `{"type": "function", "function": {...}}`，有的返回 `{"name": ..., "description": ..., "parameters": {...}}`。

---

## 验证方式

1. `PYTHONHASHSEED=random python -c "from agent.core.vector_memory import simple_text_hash; print(simple_text_hash('test')[0])"` 连续运行两次，确认结果不同
2. `python demo.py` 确认 mock 模式不崩溃
3. 运行 `pytest` 全量测试确认回归
4. 用 `echo 'ls && echo pwned' | coding-agent` 测试命令注入是否被拦截
5. `curl "http://127.0.0.1:18792/completion/stream?task=test"` 确认 CORS 头
