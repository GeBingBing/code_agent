"""Tests for the refactored VectorMemory with pluggable providers (PR-04)."""

import numpy as np
import pytest
from pathlib import Path

from agent.core.vector_memory import (
    VectorMemory,
    SearchHit,
    get_vector_memory,
    reset_vector_memory,
)
from agent.core.embeddings import HashingEmbeddingProvider


# ── Provider injection ────────────────────────────────────────────


class TestProviderInjection:
    def test_default_provider_is_hashing(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "x.db")
        assert isinstance(vm.provider, HashingEmbeddingProvider)

    def test_custom_provider_accepted(self, tmp_path):
        custom = HashingEmbeddingProvider(dim=64)
        vm = VectorMemory(db_path=tmp_path / "x.db", provider=custom)
        assert vm.provider is custom
        assert vm.dim == 64

    def test_provider_can_be_replaced_between_instances(self, tmp_path):
        v1 = VectorMemory(db_path=tmp_path / "x.db", provider=HashingEmbeddingProvider(dim=32))
        v1.add("k", "v")
        v2 = VectorMemory(db_path=tmp_path / "x.db", provider=HashingEmbeddingProvider(dim=64))
        # Different dim provider can read existing data
        assert v2.count() == 1


# ── Search hit dataclass ─────────────────────────────────────────


class TestSearchHit:
    def test_to_dict_default_includes_metadata(self):
        h = SearchHit(doc_id=1, key="k", value="v", score=0.9, metadata={"src": "test"})
        d = h.to_dict()
        assert d["id"] == 1
        assert d["key"] == "k"
        assert d["value"] == "v"
        assert d["score"] == 0.9
        assert d["metadata"] == {"src": "test"}

    def test_to_dict_excludes_metadata(self):
        h = SearchHit(doc_id=1, key="k", value="v", score=0.5, metadata={"src": "test"})
        d = h.to_dict(include_metadata=False)
        assert "metadata" not in d

    def test_to_dict_no_metadata(self):
        h = SearchHit(doc_id=2, key="k", value="v", score=0.3)
        d = h.to_dict()
        assert d["metadata"] == {}

    def test_score_rounded(self):
        h = SearchHit(doc_id=1, key="k", value="v", score=0.123456789)
        d = h.to_dict()
        assert d["score"] == 0.1235  # 4 decimal places


# ── Add + search ─────────────────────────────────────────────────


class TestAddAndSearch:
    @pytest.fixture
    def vm(self, tmp_path):
        return VectorMemory(db_path=tmp_path / "test.db", provider=HashingEmbeddingProvider(dim=128))

    def test_add_returns_id(self, vm):
        id_ = vm.add("k", "value")
        assert isinstance(id_, int)
        assert id_ > 0

    def test_add_with_metadata_persists(self, vm):
        id_ = vm.add("k", "v", metadata={"source": "test", "tag": "x"})
        hit = vm.search("k", top_k=1, return_hits=True)[0]
        assert hit.doc_id == id_
        assert hit.metadata == {"source": "test", "tag": "x"}

    def test_add_with_precomputed_embedding(self, vm, tmp_path):
        custom_vec = np.ones(128, dtype=np.float32)
        custom_vec /= np.linalg.norm(custom_vec)
        vm.add("k", "v", embedding=custom_vec)
        hit = vm.search("anything", top_k=1, return_hits=True)[0]
        assert hit.doc_id is not None

    def test_search_returns_hits_sorted_descending(self, vm):
        vm.add("alpha", "the quick brown fox")
        vm.add("beta", "the lazy dog sleeps")
        vm.add("gamma", "completely unrelated content about cooking")
        hits = vm.search("dog", top_k=10, return_hits=True)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_search_top_k_limits(self, vm):
        for i in range(10):
            vm.add(f"k{i}", f"value {i}")
        hits = vm.search("value", top_k=3, return_hits=True)
        assert len(hits) == 3

    def test_search_default_returns_tuples(self, vm):
        vm.add("k", "v")
        results = vm.search("v", top_k=1)
        assert isinstance(results, list)
        assert len(results) == 1
        key, value, score = results[0]
        assert key == "k"
        assert value == "v"
        assert isinstance(score, float)

    def test_search_empty_db(self, vm):
        results = vm.search("anything", top_k=5, return_hits=True)
        assert results == []

    def test_search_scores_in_range(self, vm):
        vm.add("k", "test value")
        hits = vm.search("test", top_k=1, return_hits=True)
        assert -1.0 <= hits[0].score <= 1.0


# ── Schema migration ─────────────────────────────────────────────


class TestSchemaMigration:
    def test_existing_db_without_dim_column_is_migrated(self, tmp_path):
        """Pre-PR-04 DBs lack the `dim` column. Loading them should auto-migrate."""
        import sqlite3
        db = tmp_path / "old.db"
        # Create an old-style table
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL,
                accessed_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
        # Now load it via VectorMemory — should auto-migrate and accept writes
        vm = VectorMemory(db_path=db)
        vm.add("k", "v")
        assert vm.count() == 1
        hits = vm.search("v", top_k=1, return_hits=True)
        assert len(hits) == 1


# ── Singleton ────────────────────────────────────────────────────


class TestSingletonWithProvider:
    def test_reset_clears_singleton(self):
        reset_vector_memory()
        v1 = get_vector_memory()
        v1.add("k", "v")
        reset_vector_memory()
        v2 = get_vector_memory()
        assert v2.count() == 0

    def test_singleton_reuses_provider(self):
        reset_vector_memory()
        v1 = get_vector_memory(provider=HashingEmbeddingProvider(dim=64))
        v2 = get_vector_memory()  # No provider — should reuse existing
        assert v1 is v2
        assert v2.dim == 64


# ── Persistence round-trip ───────────────────────────────────────


class TestPersistence:
    def test_data_persists_across_instances(self, tmp_path):
        v1 = VectorMemory(db_path=tmp_path / "persist.db")
        v1.add("k1", "value one")
        v1.add("k2", "value two")
        # Reopen
        v2 = VectorMemory(db_path=tmp_path / "persist.db")
        assert v2.count() == 2
        hits = v2.search("value", top_k=2, return_hits=True)
        assert len(hits) == 2
