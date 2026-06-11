"""Vector Memory — local vector store with pluggable embedding provider (PR-04).

Supports:
- Pluggable EmbeddingProvider (PR-04: Hashing | SentenceTransformer | TF-IDF)
- Cosine similarity search
- Persistent SQLite storage
- Auto-expiry / cleanup

Backwards compatibility:
- `simple_text_hash(text, dim=128)` and `cosine_similarity(a, b)` remain
  importable from this module — they're just wrappers around HashingEmbeddingProvider.
- `VectorMemory(key, value, ...)` API unchanged.
- `get_vector_memory()` / `reset_vector_memory()` singletons unchanged.

The default provider is the deterministic HashingEmbeddingProvider (same
algorithm as before, so existing memory.db files remain readable). Callers
can opt into a richer provider by passing one explicitly to the constructor
or by setting `EMBEDDING_PROVIDER=sentence-transformers` in the environment.
"""

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    get_default_provider,
)


# ── Backwards-compat helpers (used by other modules) ───────────────


def _word_hash(word: str, dim: int) -> int:
    """Deterministic hash for a single word using SHA-256.

    Uses hashlib instead of Python's built-in hash() because hash()
    is randomized per-interpreter-invocation (PYTHONHASHSEED),
    which would break cross-session vector search.
    """
    digest = hashlib.sha256(word.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % dim


def simple_text_hash(text: str, dim: int = 128) -> np.ndarray:
    """Bag-of-words SHA-256 hashing embedding. L2-normalized.

    Retained for backwards compatibility with code that imports it directly.
    Equivalent to `HashingEmbeddingProvider(dim).encode(text)` as a numpy array.
    """
    provider = HashingEmbeddingProvider(dim=dim)
    return np.asarray(provider.encode(text), dtype=np.float64)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for L2-normalized vectors → dot product."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ── Search result ──────────────────────────────────────────────────


@dataclass
class SearchHit:
    """A single semantic search hit."""
    doc_id: int
    key: str
    value: str
    score: float
    metadata: Optional[dict] = None

    def to_dict(self, include_metadata: bool = True) -> dict:
        d = {
            "id": self.doc_id,
            "key": self.key,
            "value": self.value,
            "score": round(float(self.score), 4),
        }
        if include_metadata:
            d["metadata"] = self.metadata or {}
        return d


# ── Main class ─────────────────────────────────────────────────────


class VectorMemory:
    """Vector memory store with pluggable embedding backend.

    L3 long-term memory's semantic search engine.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        provider: Optional[EmbeddingProvider] = None,
    ):
        """Initialize vector memory.

        Args:
            db_path: SQLite database path (default: ~/.coding-agent/vector_memory.db)
            provider: EmbeddingProvider to use. If None, resolves from
                      `EMBEDDING_PROVIDER` env var (default: auto → hashing).
        """
        self.memory_dir = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent"))
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or self.memory_dir / "vector_memory.db"

        self.db = sqlite3.connect(str(self.db_path))
        self.db.row_factory = sqlite3.Row
        self.provider: EmbeddingProvider = provider or get_default_provider()
        self._init_db()

    def _init_db(self):
        """Initialize database tables; apply migrations to existing DBs."""
        cursor = self.db.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dim INTEGER NOT NULL DEFAULT 128,
                created_at TEXT NOT NULL,
                accessed_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 0
            )
        """)
        # Schema migrations: add columns that older DBs may not have.
        self._ensure_column(cursor, "memories", "dim", "INTEGER NOT NULL DEFAULT 128")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_key ON memories(key)
        """)
        # FTS5 for keyword fallback (provider-agnostic)
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(key, value, content='memories', content_rowid='id')
        """)
        self.db.commit()

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        """Add a column to an existing table if it doesn't exist (idempotent)."""
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        if column not in existing:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass  # Concurrent migration; ignore.

    def add(
        self,
        key: str,
        value: str,
        embedding: Optional[np.ndarray] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Add a memory.

        Args:
            key: Memory key/tag (e.g. "last_written_file")
            value: Memory text content
            embedding: Pre-computed vector. If None, the configured provider encodes `value`.
            metadata: Optional JSON-serializable metadata.

        Returns:
            Memory row ID
        """
        if embedding is None:
            vec_list = self.provider.encode(value)
        else:
            vec_list = np.asarray(embedding, dtype=np.float32).tolist()
        vec = np.asarray(vec_list, dtype=np.float32)
        emb_bytes = vec.tobytes()
        dim = vec.size

        now = datetime.now().isoformat()
        cursor = self.db.cursor()
        cursor.execute(
            """INSERT INTO memories (key, value, embedding, dim, created_at, accessed_at, access_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (key, value, emb_bytes, dim, now, now),
        )
        # FTS5 mirror (best-effort)
        try:
            cursor.execute(
                "INSERT INTO memories_fts (rowid, key, value) VALUES (?, ?, ?)",
                (cursor.lastrowid, key, value),
            )
        except sqlite3.OperationalError:
            pass  # FTS5 may not be compiled in; ignore.

        # Metadata sidecar (optional, lazy table)
        if metadata:
            self._store_metadata(cursor.lastrowid, metadata)

        self.db.commit()
        return cursor.lastrowid

    def _store_metadata(self, doc_id: int, metadata: dict) -> None:
        """Persist metadata as a sidecar (best-effort, JSON)."""
        cursor = self.db.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                id INTEGER PRIMARY KEY,
                metadata TEXT NOT NULL
            )
        """)
        cursor.execute(
            "INSERT OR REPLACE INTO memory_meta (id, metadata) VALUES (?, ?)",
            (doc_id, json.dumps(metadata, default=str)),
        )

    def _load_metadata(self, doc_id: int) -> Optional[dict]:
        cursor = self.db.cursor()
        try:
            cursor.execute("SELECT metadata FROM memory_meta WHERE id = ?", (doc_id,))
        except sqlite3.OperationalError:
            return None
        row = cursor.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["metadata"])
        except (json.JSONDecodeError, KeyError):
            return None

    def search(
        self,
        query: str,
        top_k: int = 5,
        return_hits: bool = False,
    ):
        """Semantic search over stored memories.

        Uses the configured provider to embed the query, then ranks all
        stored memories by cosine similarity.

        Args:
            query: Search query text
            top_k: Number of results to return
            return_hits: If True, return List[SearchHit]; else List[(key, value, score)]

        Returns:
            List of results, sorted by descending similarity.
        """
        q_vec = np.asarray(self.provider.encode(query), dtype=np.float32)

        cursor = self.db.cursor()
        cursor.execute("SELECT id, key, value, embedding, dim FROM memories")
        rows = cursor.fetchall()

        hits: List[SearchHit] = []
        for row in rows:
            try:
                doc_dim = int(row["dim"])
                doc_vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if doc_vec.size != doc_dim:
                    # Schema drift (old rows stored as float64). Re-interpret.
                    doc_vec = np.frombuffer(row["embedding"], dtype=np.float64)
                sim = cosine_similarity(q_vec, doc_vec)
                hits.append(
                    SearchHit(
                        doc_id=int(row["id"]),
                        key=row["key"],
                        value=row["value"],
                        score=sim,
                        metadata=self._load_metadata(int(row["id"])),
                    )
                )
            except Exception:
                # Skip malformed rows; don't fail the whole search
                continue

        # Sort by descending similarity
        hits.sort(key=lambda h: h.score, reverse=True)
        top = hits[:top_k]

        # Bump access stats for the returned results
        for h in top:
            cursor.execute(
                "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                (datetime.now().isoformat(), h.doc_id),
            )
        self.db.commit()

        if return_hits:
            return top
        return [(h.key, h.value, h.score) for h in top]

    def get(self, key: str) -> Optional[str]:
        """Get the most recent value for a key."""
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT value FROM memories WHERE key = ? ORDER BY created_at DESC LIMIT 1",
            (key,),
        )
        row = cursor.fetchone()
        return row["value"] if row else None

    def get_all_keys(self) -> List[str]:
        """List all distinct keys (most-recently-accessed first)."""
        cursor = self.db.cursor()
        cursor.execute("SELECT DISTINCT key FROM memories ORDER BY accessed_at DESC")
        return [row["key"] for row in cursor.fetchall()]

    def delete_old(self, max_age_days: int = 30) -> int:
        """Delete memories older than max_age_days."""
        cursor = self.db.cursor()
        cutoff = datetime.now().timestamp() - (max_age_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()
        cursor.execute("DELETE FROM memories WHERE created_at < ?", (cutoff_iso,))
        deleted = cursor.rowcount
        self.db.commit()
        return deleted

    def clear(self):
        """Erase all memories."""
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM memories")
        try:
            cursor.execute("DELETE FROM memory_meta")
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def count(self) -> int:
        """Total number of stored memories."""
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM memories")
        return cursor.fetchone()["cnt"]

    @property
    def dim(self) -> int:
        """Embedding dimension of the configured provider."""
        return self.provider.dim


# ── Singleton ──────────────────────────────────────────────────────

_vector_memory: Optional[VectorMemory] = None


def get_vector_memory(provider: Optional[EmbeddingProvider] = None) -> VectorMemory:
    """Get the global VectorMemory singleton.

    Args:
        provider: Optional override for the embedding provider. If None,
                  uses `EMBEDDING_PROVIDER` env var (default: auto).
    """
    global _vector_memory
    if _vector_memory is None:
        _vector_memory = VectorMemory(provider=provider)
    return _vector_memory


def reset_vector_memory():
    """Reset the global vector memory (for tests)."""
    global _vector_memory
    if _vector_memory is not None:
        try:
            _vector_memory.clear()
        except Exception:
            pass
    _vector_memory = None
