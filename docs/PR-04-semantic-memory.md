# PR-04: 真实语义记忆（本地 Embedding 替换 TF-IDF）

> 关联：SPECS.md Phase 12-4 | 状态：待实施 | 决策：已确认
> 依据：[docs/1.md §5.2 上下文工程管道](../1.md) | [docs/参考.md 上下文管理 Aider repomap](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Embedding 模型 | `all-MiniLM-L6-v2` (sentence-transformers) | 90MB，本地 CPU 推理 <50ms |
| 向量维度 | 384 | 与 MiniLM 输出一致 |
| 存储 | 现有 SQLite + numpy（不引入新 DB） | 最小改动 |
| 搜索接口 | `semantic_search(query, k=10)` | 与现有 `L3 长期记忆` 接口对齐 |
| 离线降级 | TF-IDF 兜底（当 sentence-transformers 不可用） | 不强制依赖 |
| 索引更新 | mtime 触发（启动时扫描 + 写文件时） | 增量而非全量 |

---

## 现状 / 目标

**现状**（`agent/core/vector_memory.py` + `agent/core/memory.py`）：
- "向量记忆形同虚设"（docs/gap-analysis.md 评语）
- 实际是 **TF-IDF 词袋模型**——按词频匹配，不是语义
- 搜索"如何处理并发"找不到包含"async/await"、"asyncio.gather"的记忆（因为词频 0）
- 之前修过 hash 随机化 bug，但本质问题没解决

**目标**（1.md §5.3 三层记忆）：
- 长期记忆用真实 embedding，搜索返回**语义相关**的结果
- 与工作记忆 / 会话记忆分层清晰
- 离线场景：模型加载失败时降级到 TF-IDF（warn 用户）

---

## 设计

### Embedding 抽象层

```python
# agent/core/embeddings.py (新文件)

class EmbeddingProvider(Protocol):
    def encode(self, text: str) -> list[float]: ...
    @property
    def dim(self) -> int: ...


class SentenceTransformerProvider:
    """生产实现：使用 sentence-transformers MiniLM."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name)
        self._dim = self._model.get_sentence_embedding_dimension()

    def encode(self, text: str) -> list[float]:
        return self._model.encode(text).tolist()

    @property
    def dim(self) -> int:
        return self._dim


class TfidfFallbackProvider:
    """降级实现：原 TF-IDF 词袋模型。"""

    def __init__(self, dim: int = 384):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self._vec = TfidfVectorizer(max_features=dim)
        self._dim = dim
        self._fitted = False

    def encode(self, text: str) -> list[float]:
        # 首次调用时 fit，后续 transform
        ...


def get_default_provider() -> EmbeddingProvider:
    try:
        return SentenceTransformerProvider()
    except ImportError:
        logger.warning("sentence-transformers not installed; using TF-IDF fallback")
        return TfidfFallbackProvider()
```

### VectorStore 升级

```python
# agent/core/vector_memory.py 修改

class VectorStore:
    """SQLite + numpy 向量存储，支持 cosine similarity."""

    def __init__(self, db_path: Path, provider: EmbeddingProvider = None):
        self.db_path = db_path
        self.provider = provider or get_default_provider()
        self._init_db()

    def add(self, doc_id: str, text: str, metadata: dict = None) -> None:
        """存储文档 + embedding."""
        vec = np.array(self.provider.encode(text), dtype=np.float32)
        self._conn.execute(
            "INSERT OR REPLACE INTO docs (id, text, vec, metadata) VALUES (?, ?, ?, ?)",
            (doc_id, text, vec.tobytes(), json.dumps(metadata or {}))
        )
        self._conn.commit()

    def search(self, query: str, k: int = 10) -> list[SearchHit]:
        """语义搜索：返回 top-k 相似文档."""
        q_vec = np.array(self.provider.encode(query), dtype=np.float32)
        results = []
        for row in self._conn.execute("SELECT id, text, vec, metadata FROM docs"):
            doc_vec = np.frombuffer(row[2], dtype=np.float32)
            sim = self._cosine_sim(q_vec, doc_vec)
            results.append(SearchHit(
                doc_id=row[0], text=row[1], score=sim,
                metadata=json.loads(row[3])
            ))
        return sorted(results, key=lambda h: -h.score)[:k]

    def _cosine_sim(self, a, b) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))
```

### L3 记忆工具

```python
# agent/tools/memory.py (新文件) 或挂到现有

class SemanticSearchTool(BaseTool):
    name = "semantic_search"
    description = "Search long-term memory semantically. Returns top-k relevant past memories."

    async def execute(self, query: str, k: int = 5, **kwargs) -> ToolResult:
        hits = self.vector_store.search(query, k=k)
        return ToolResult(output=json.dumps([h.to_dict() for h in hits], indent=2))
```

### 离线降级

```python
# agent/core/config.py 增加
class AgentConfig:
    embedding_provider: str = "auto"  # "auto" | "sentence-transformers" | "tfidf"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/embeddings.py` | **新建** — EmbeddingProvider Protocol + SentenceTransformerProvider + TfidfFallbackProvider + get_default_provider |
| `agent/core/vector_memory.py` | 重写为依赖 EmbeddingProvider；增加 `search(query, k)` 方法（替换原 TF-IDF 算法） |
| `agent/core/memory.py` | L3 长期记忆使用新 VectorStore |
| `agent/tools/__init__.py` | 注册 `semantic_search` 工具 |
| `agent/tools/memory.py` | **新建** — SemanticSearchTool |
| `agent/core/config.py` | embedding_provider / embedding_model / embedding_dim 配置 |
| `pyproject.toml` | `sentence-transformers` 依赖（可选 `[embeddings]` extra） |
| `tests/test_embeddings.py` | **新建** — SentenceTransformerProvider 加载、TfidfFallback 降级 |
| `tests/test_vector_memory.py` | **新建** — add/search 语义相关性、cosine similarity、k 截断 |
| `tests/test_semantic_search_quality.py` | **新建** — 搜索"并发"能找到含"async/await"的内容（基准测试） |

---

## 验收标准

- [ ] `SentenceTransformerProvider` 加载 `all-MiniLM-L6-v2`，encode "hello world" 返回 384 维向量
- [ ] `TfidfFallbackProvider` 在 sentence-transformers 缺失时启动
- [ ] `VectorStore.search("如何处理并发")` 返回含 "async/await" / "asyncio.gather" 的记忆（即使字面无"并发"）
- [ ] `VectorStore.search("foo")` 返回 top-k 按 cosine similarity 降序
- [ ] `semantic_search` 工具注册成功，LLM 可调用
- [ ] mtime 触发增量索引：写新文档后无需重启即可搜到
- [ ] 离线模式（不装 sentence-transformers）降级到 TF-IDF，不崩溃
- [ ] 现有 398+ 测试不回归
- [ ] 手动验证：搜索"如何处理并发"能找到含 "asyncio.gather" 的历史记忆

---

## 实施顺序

```
Step 1: agent/core/embeddings.py              (新文件，1.5h)
Step 2: tests/test_embeddings.py              (新文件，0.5h)
Step 3: agent/core/vector_memory.py 重写      (改文件，2h)
Step 4: tests/test_vector_memory.py           (新文件，1h)
Step 5: agent/tools/memory.py                 (新文件，1h)
Step 6: agent/tools/__init__.py               (改文件，0.5h)
Step 7: agent/core/memory.py 集成              (改文件，1h)
Step 8: agent/core/config.py                  (改文件，0.5h)
Step 9: tests/test_semantic_search_quality.py (新文件，0.5h)
Step 10: pytest tests/ 验证                    (0.5h)
```

总工作量：~9h

**前置依赖**：无

---

## 与其他 PR 的关系

- 与 PR-05 repomap：repomap 注入文件结构，semantic memory 注入历史经验
- 与 PR-07 Orchestrator：Orchestrator 子 agent 可调用 `semantic_search` 检索过往相似任务
- 与 PR-09 Evaluator：Evaluator 评分时调用 `semantic_search` 找"类似任务的处理方式"
