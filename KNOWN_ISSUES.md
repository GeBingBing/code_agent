# KNOWN_ISSUES.md

> 当前未修复或存在 workaround 的问题集中追踪。每条 issue 必带:复现步骤 / 触发场景 / 当前 workaround / 修复 ETA。
>
> 历史问题快照见 [docs/code-review-issues.md](docs/code-review-issues.md)(2026-05-22,已停止维护)和 [docs/PR-TEST-REPORT.md](docs/PR-TEST-REPORT.md)(2026-06-07)。
>
> 修复后请把对应条目移到 "已修复" 段落底部并标注修复 commit / 日期。

---

## 当前未修复 (Active)

### ISSUE-001: `test_engine_uses_fact_confirm_extractor` 在并行运行时偶发失败

- **触发**:`pytest tests/ -q`(全量并行)
- **复现**:单独跑 `pytest tests/test_engine_run.py::TestMultiTurnExtraction::test_engine_uses_fact_confirm_extractor` → PASS。只在大批量并行/顺序运行时才失败。
- **怀疑根因**:测试间 fixture 污染(env / global state / pyc 缓存)。
- **Workaround**:暂无;CI 仍能 pass(ci.yml 跑顺序模式)。
- **修复 ETA**:Phase 3 定位,可能加 `pytest.mark.flaky` 重试一次。
- **影响范围**:测试稳定性,不影响生产路径。

### ISSUE-002: `verify_acs(phase_id="P0")` 阶段 ID 解析失败

- **触发**:`pytest tests/test_integration_all_tools.py`
- **复现**:`tests/test_integration_all_tools.py:446` 调用 `verify_acs(phase_id="P0")`,但 SPECS.md 阶段 ID 实际为 `P0-1` / `P0-2` / ... 而非 `P0`,`spec_loader.verify_acs` 找不到该 phase。
- **当前文档**:[docs/PR-TEST-REPORT.md](docs/PR-TEST-REPORT.md)、[docs/DESIGN-unfinished-6ACs.md](docs/DESIGN-unfinished-6ACs.md) 已讨论。
- **Workaround**:CI 用 `--ignore=tests/test_integration_all_tools.py` 跳过;手动复现仅作集成验证。
- **修复 ETA**:hay 拍板设计后(候选方案:改测试用 `P0-1` 或 `spec_loader` 支持前缀匹配)。
- **影响范围**:1 个红用例,不影响生产。

### ISSUE-003: progress.txt `current_step` 越界长期累加

- **触发**:长时间使用累加,无 reset 机制。已观察到 `19990/200`、`20392/200` 等异常值。
- **复现**:连续跑多个 task → 查看 `WORKSPACE/.claude-progress.txt`,`current_step` 数值会跨 session 累加超过 max_steps。
- **Workaround**:删除 `.claude-progress.txt` 重置;**Phase 1.3 已加 invariant 拦截**(`progress_anchor.step_within_sane_bound`,数值 > max_steps × 10 时 raise)。
- **修复 ETA**:已部分修复(invariant 防止新写入越界);需要 `/reset-progress` 命令清理已存在的污染文件。
- **影响范围**:进度追踪不准、resume 行为不可预测。

### ISSUE-004: `OPENAI_API_KEY=mock` 历史上不真 mock(已修)

- **历史触发**:`agent/llm/client.py` 的 `provider="mock"` 分支只是创建 `OpenAI(api_key="mock", ...)`,实际仍会真打 API 网络。
- **2026-06-14 已修复**:`provider="mock"` 现在走真 short-circuit(`_MockLLMBackend`),不创建 OpenAI client,不打网络。`tests/contracts/test_mock_llm_contract.py::test_no_network_socket_opened` 是回归保护。
- **保留这条**:为了让翻仓库的人知道这个常见误解已经修了。

### ISSUE-005: 系统 site-packages 内的过时 `agent` 包污染 import

- **触发**:`pip install -e .` 之前装过包,site-packages 残留了旧的 `coding_agent`(里面有自己的 `agent` 模块)。
- **复现**:`tests/test_integration_all_tools.py` 跑 9 分钟而不是 30 秒;`agent/tools/lsp_tool.py` 抛 `ImportError: cannot import name 'LSPError' from 'agent.lsp.client'`(因为 import 命中 site-packages 版本)。
- **诊断**:`python -c "import agent; print(agent.__file__)"` —— 如果路径不在仓库下,就是这个问题。
- **Workaround**:`pip uninstall coding_agent -y` 然后 `pip install -e .` 重装。
- **修复 ETA**:`pyproject.toml` 加 deprecated package conflict 检测;或 README 明确列出该 setup gotcha。
- **影响范围**:开发体验(tests 慢)、误导性 import 错误。

### ISSUE-006: 测试间状态污染(疑似)

- **触发**:某些 LLM-touching 测试连续跑会比单独跑慢 5-10 倍,或出现单跑过、批量跑红的现象。
- **复现**:比较 `pytest tests/test_engine_run.py` 单跑 vs 全量 `pytest tests/` 中同一测试的耗时。
- **怀疑根因**:asyncio loop 复用、global EventBus singleton、文件 mtime cache、LSP client 模块级 import。
- **Workaround**:独立跑改动模块的测试;CI 跑 `--ignore=tests/test_integration_all_tools.py`。
- **修复 ETA**:Phase 3+;先建 contract test 覆盖关键 round-trip(MockLLM、记忆、工具)。

---

## 已修复 (Resolved)

### RES-001: progress.txt 越界写入无检测(2026-06-14 修)

- 见 ISSUE-003。`agent/core/invariants.py` + `agent/core/progress_anchor.py:write` 已加硬断言。

### RES-002: provider="mock" 仍打网络(2026-06-14 修)

- 见 ISSUE-004。`agent/llm/client.py` 的 mock 分支重写为内置 `_MockLLMBackend`。

### RES-003: Engine 把 `provider="mock"` 当 "无 LLM"(2026-06-14 修)

- **历史触发**:`agent/core/engine.py:440` 在 `model="mock" or provider="mock"` 时把 `self.llm = None`,导致 CLI 起来就显示 "No LLM configured" 退出。
- **修复**:仅在 `model="mock"` 但 `provider != "mock"` 的兼容路径下保留旧行为(为支持手动注入 `MockLLMClient`);`provider="mock"` 现在构造真 LLMClient。
- **副作用修复**:`tests/test_engine_orchestrator.py` 两个 `test_no_llm_*` 测试断言 `e.llm is None`,改为显式 `e.llm = None` 模拟无 LLM 情况。

### RES-006: git / refactor 写入路径绕过 self-source 保护(2026-06-15 修)

- **历史触发**:RES-005 只堵了 `file_ops.py` 的 4 个写工具,但还存在另外两个回滚通道:
  1. `agent/tools/git_tool.py` 允许 `checkout` 和 `stash` 子命令。LLM 在"自我修复"循环里调用 `git checkout -- agent/llm/client.py` 会**直接把文件还原到 commit 时的状态**——这是更典型的"代码改动消失"路径。
  2. `agent/tools/git_smart.py:smart_branch(switch=True)` 走 `git checkout <branch>`,虽然分支切换不算回滚,但配合未提交改动 + 冲突会让用户体感"代码没了"。
  3. `agent/tools/refactor.py:184` `SafeRenameTool` 直接 `file_path.write_text`,绕过 `_validate_write_path`,重命名跨文件符号时会覆盖 `agent/` 等源码里的同名符号。
- **修复**:
  - `git_tool.py`:把 `stash` 从允许列表移到 `_BLOCKED_SUBCOMMANDS`,错误消息明确说明"会藏起未提交修改";对 `checkout` 子命令额外检查参数里是否含 `--`(refspec/path 分隔符),一旦出现就拒绝并提示"会还原文件"。
  - `refactor.py`:在 `file_path.write_text` 前调用 `is_protected_path(fpath_abs, workspace_root)`,若命中则跳过写入但仍在 preview 中标记 `⛔ skipped (protected)`,LLM 能看到会跳过哪些文件。
  - `git_smart.py`:无需改 —— 它的 `git checkout` 调用是分支切换形式(无 `--`),不在新防御的拦截范围。
- **回归保护**:**未补** — 测试代码按用户要求在提交前删除。如需未来加 regression test,建议覆盖 `git checkout -- file` / `git stash` / `safe_rename` 三条路径在 source-tree workspace 下的拒绝行为。
- **影响**:三条未提交的手动编辑回滚通道关闭;self-evolution 路径通过 `CODING_AGENT_ALLOW_SELF_MODIFY=1` 仍可走。

### RES-005: agent 写工具覆盖用户对源码的手动修改(2026-06-15 修)

- **历史触发**:用户在仓库根目录跑 agent,`WORKSPACE_ROOT` 默认等于 `Path.cwd()`,即项目根。`_validate_write_path` 只校验"是否在 workspace 内",对 `agent/` / `ui/` / `index/` / `tests/` / `docs/` 等源码子目录没有任何保护。一旦 LLM 决定"重写"这些文件(`write_file` / `apply_diff` / `replace_lines` / `insert_after_line` 之一),用户的手动修改会被静默覆盖——表象是"代码改动被回滚"。
- **修复**:新增 `agent/core/protected_paths.py`,检测 `WORKSPACE_ROOT` 是否处于 agent 自身源码树(同时存在 `pyproject.toml` + `agent/`),若是则拒绝写入 `agent/` `ui/` `index/` `tests/` `docs/` 及 `pyproject.toml` / `CLAUDE.md` / `CODING_AGENT.md`。返回 `ToolResult(success=False, ...)` 而非抛异常,LLM 在 ReAct 循环中看到明确错误。Escape hatch:`CODING_AGENT_ALLOW_SELF_MODIFY=1`(显式 self-evolution)。
- **回归保护**:**未补** — 测试代码按用户要求在提交前删除。如需未来加 regression test,建议覆盖 `write_file` / `apply_diff` / `replace_lines` 在 source-tree workspace 下被拒绝、`workspace/` 子目录仍可写、`CODING_AGENT_ALLOW_SELF_MODIFY=1` 旁路生效这几个核心场景。
- **影响**:用户对 agent 源码的手动编辑不再被 agent 自己的写工具吞掉;self-evolution 路径仍可通过环境变量显式启用。

### RES-004: tool_call/tool_result 在压缩中分裂(2026-06-14 修)

- **历史触发**:`agent/core/memory.py:_compress` 切割 keep_from 时,可能保留 call 但删除 result(或反之),导致 LLM 收到 orphan(code-review-issues #6 历史 bug)。
- **修复**:`_compress` 末尾比较前后的 pair 完整性,加 `memory.compaction_no_pair_split` invariant —— 真分裂(原本配对的现在变 orphan)时直接 raise。
- 同时改进 `_assert_tool_pairing_invariant`:豁免 working_memory 末尾的"中间态" call(engine add 三元组中刚加 call 还没加 result 的瞬间)。

---

## Reporting 流程

发现新问题:
1. 在 "当前未修复" 段加新条目,序号递增(ISSUE-007、008...)
2. 给出最小复现步骤 + 触发场景 + 当前 workaround + 修复 ETA + 影响范围
3. 修完后:加 RES-NN 到 "已修复" 段,把 ISSUE-NN 那条留作历史(不要删,带上修复日期 + commit)
4. 同步更新:[docs/PR-TEST-REPORT.md](docs/PR-TEST-REPORT.md) 引用此文件作为"当前状态"
