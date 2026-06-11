# PR-05: repomap 注入（Aider codmap 风格）

> 关联：SPECS.md Phase 13-1 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §5.2 上下文工程管道](../1.md) | [docs/参考.md 上下文管理 Aider repomap / MoAI-ADK codmap](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 实现风格 | Aider repomap + MoAI-ADK codmap 混合 | Aider 提供文件级，codmap 提供模块级 |
| 注入位置 | system-reminder（不是 system prompt） | 不破坏 prompt cache（与 cwd 同层） |
| 触发时机 | 每次 LLM call 前（如果 mtime 变化） | 与现有 1.md §5.2 描述一致 |
| 文件数限制 | 50 个文件 / 200 行 / 5KB 总大小 | 避免爆 context |
| 排序 | 按最近修改时间 + 大小 | 重要文件优先 |

---

## 现状 / 目标

**现状**：
- LLM 不知道项目结构，每次都要 `list_files` 探索
- 探索消耗 N 步（每步都要 tool call）
- 即使 `code_indexer` 已索引，system prompt 里没有项目级"鸟瞰图"

**目标**（1.md §5.2）：
> **地图注入**：始终注入一份可读的代码库地图（类似 `repomap`、`codmap`），标注文件大小和最近修改时间

输出格式：
```
src/auth/login.py (340 lines, mod 2d ago)
  class AuthService:
    def verify_token(token: str) -> bool
src/api/users.py (120 lines, mod 1d ago)
  def get_users() -> list[User]
  def create_user(data: dict) -> User
```

---

## 设计

### Repomap 生成器

```python
# index/codmap.py (新文件)

@dataclass
class FileEntry:
    path: str
    line_count: int
    mtime: float
    symbols: list[str] = field(default_factory=list)  # 顶层符号签名

    def to_line(self) -> str:
        age = self._age_str()
        return f"{self.path} ({self.line_count} lines, {age})"


class CodmapGenerator:
    """生成项目代码地图。增量：基于 mtime 缓存。"""

    def __init__(self, workspace: Path, indexer: CodeIndexer = None):
        self.workspace = workspace
        self.indexer = indexer or CodeIndexer()
        self._cache: dict[str, FileEntry] = {}
        self._cache_mtime: dict[str, float] = {}

    def generate(self, max_files: int = 50, max_total_kb: int = 5) -> str:
        """Generate a readable codemap string."""
        entries = self._scan()
        # Sort: most recently modified first
        entries.sort(key=lambda e: -e.mtime)
        # Truncate
        entries = entries[:max_files]
        lines = []
        total_bytes = 0
        for entry in entries:
            line = entry.to_line()
            for sym in entry.symbols[:5]:  # max 5 symbols per file
                line += f"\n  {sym}"
            line_bytes = len(line.encode())
            if total_bytes + line_bytes > max_total_kb * 1024:
                break
            lines.append(line)
            total_bytes += line_bytes
        return "\n".join(lines)

    def _scan(self) -> list[FileEntry]:
        """扫描 workspace 下所有代码文件。"""
        result = []
        for path in self.workspace.rglob("*"):
            if not path.is_file() or not self._is_code(path):
                continue
            stat = path.stat()
            mtime = stat.st_mtime
            if self._cache_mtime.get(str(path)) == mtime:
                # 缓存命中
                result.append(self._cache[str(path)])
                continue
            entry = FileEntry(
                path=str(path.relative_to(self.workspace)),
                line_count=self._count_lines(path),
                mtime=mtime,
                symbols=self._extract_symbols(path),
            )
            self._cache[str(path)] = entry
            self._cache_mtime[str(path)] = mtime
            result.append(entry)
        return result

    def _is_code(self, path: Path) -> bool:
        return path.suffix in {".py", ".js", ".ts", ".go", ".rs", ".java"}

    def _count_lines(self, path: Path) -> int:
        try:
            return sum(1 for _ in path.open("rb"))
        except (OSError, UnicodeDecodeError):
            return 0

    def _extract_symbols(self, path: Path) -> list[str]:
        if path.suffix == ".py":
            return self.indexer.extract_python_symbols(path)
        elif path.suffix in {".js", ".ts"}:
            return self.indexer.extract_js_symbols(path)
        return []
```

### 集成到 Engine

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ...):
        self.codmap = CodmapGenerator(workspace=WORKSPACE)
        # Hook: before_llm_call → inject codmap into system-reminder
        self.hooks.register("before_llm_call", self._inject_codmap)

    async def _inject_codmap(self, payload):
        """在 system-reminder 里追加 codmap（不动 system prompt，保留 cache）。"""
        codmap_text = self.codmap.generate()
        if not codmap_text:
            return
        # Append to last user message as system-reminder
        messages = payload["messages"]
        if messages and messages[-1].role == "user":
            reminder = f"<system-reminder>\n<codmap>\n{codmap_text}\n</codmap>\n</system-reminder>"
            messages[-1].content = messages[-1].content + "\n" + reminder
```

### PromptAssembler 兼容

不动 `assembler.py` 的 system prompt（保持 cache），codmap 走 system-reminder 通道（与 cwd/git_status 同层）。

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `index/codmap.py` | **新建** — CodmapGenerator + FileEntry |
| `index/code_indexer.py` | 增加 `extract_python_symbols(path)` / `extract_js_symbols(path)` 方法（如果还没有） |
| `agent/core/engine.py` | 集成 codmap；注册 hook：before_llm_call（inject） |
| `agent/core/__init__.py` | export CodmapGenerator |
| `index/__init__.py` | export |
| `tests/test_codmap.py` | **新建** — 生成、排序、截断、缓存命中、符号提取 |
| `tests/test_engine_codmap.py` | **新建** — hook 触发、system-reminder 注入 |

---

## 验收标准

- [ ] `CodmapGenerator.generate()` 返回 50 文件以内 / 5KB 以内的 codmap 文本
- [ ] 排序：最近修改的在前
- [ ] 缓存：mtime 不变的文件不重新解析
- [ ] 符号提取：Python 文件提取 class/def 顶层签名（不超过 5 个）
- [ ] engine `before_llm_call` hook 把 codmap 注入到 user message 末尾的 system-reminder
- [ ] system prompt 不被修改（保留 prompt cache）
- [ ] 大型项目（1000+ 文件）扫描 < 1s
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: index/code_indexer.py 增加符号提取   (改文件，1h)
Step 2: index/codmap.py                       (新文件，2h)
Step 3: tests/test_codmap.py                  (新文件，1h)
Step 4: agent/core/engine.py 集成             (改文件，1h)
Step 5: tests/test_engine_codmap.py           (新文件，0.5h)
Step 6: pytest tests/ 验证                    (0.5h)
```

总工作量：~6h

**前置依赖**：PR-01（Hook 系统必须先存在）

---

## 与其他 PR 的关系

- 与 PR-04 semantic memory：repomap 注入**当前项目结构**（静态），semantic memory 注入**历史经验**（动态）
- 与 PR-13 progress anchor：codmap 在 system-reminder，progress 在 WORKSPACE/.claude-progress.txt
- 与 PR-06 SDD parser：codmap 是"代码地图"，SDD 是"规格地图"——两者一起注入 LLM
