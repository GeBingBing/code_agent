"""Tests for the EmbeddingProvider abstraction (PR-04)."""

import math
import numpy as np
import pytest

from agent.core.embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    SentenceTransformerProvider,
    TfidfEmbeddingProvider,
    get_default_provider,
    _sentence_transformers_available,
)


# ── Hashing provider ───────────────────────────────────────────────


class TestHashingProvider:
    def test_implements_protocol(self):
        p = HashingEmbeddingProvider(dim=64)
        assert isinstance(p, EmbeddingProvider)

    def test_dim_property(self):
        p = HashingEmbeddingProvider(dim=128)
        assert p.dim == 128

    def test_default_dim(self):
        p = HashingEmbeddingProvider()
        assert p.dim == 128

    def test_encode_returns_list_of_floats(self):
        p = HashingEmbeddingProvider(dim=64)
        v = p.encode("hello world")
        assert isinstance(v, list)
        assert len(v) == 64
        assert all(isinstance(x, float) for x in v)

    def test_encode_is_deterministic(self):
        p = HashingEmbeddingProvider(dim=128)
        v1 = p.encode("test string")
        v2 = p.encode("test string")
        assert v1 == v2

    def test_encode_is_normalized(self):
        p = HashingEmbeddingProvider(dim=128)
        v = p.encode("a b c d e f g h i j")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-5

    def test_empty_string_returns_zeros(self):
        p = HashingEmbeddingProvider(dim=64)
        v = p.encode("")
        assert all(x == 0.0 for x in v)
        assert len(v) == 64

    def test_whitespace_only_returns_zeros(self):
        p = HashingEmbeddingProvider(dim=64)
        v = p.encode("   \n\t  ")
        assert all(x == 0.0 for x in v)

    def test_different_texts_different_vectors(self):
        p = HashingEmbeddingProvider(dim=128)
        v1 = p.encode("python code")
        v2 = p.encode("javascript code")
        # Should differ (not guaranteed but very likely)
        assert v1 != v2

    def test_chinese_text_handled(self):
        p = HashingEmbeddingProvider(dim=64)
        v = p.encode("你好世界")
        assert len(v) == 64
        # Some mass should be present
        assert sum(abs(x) for x in v) > 0

    def test_cosine_similarity_self(self):
        p = HashingEmbeddingProvider(dim=128)
        v = np.asarray(p.encode("hello"), dtype=np.float64)
        sim = float(np.dot(v, v) / (np.linalg.norm(v) * np.linalg.norm(v)))
        assert abs(sim - 1.0) < 1e-5


# ── SentenceTransformer provider ───────────────────────────────────


class TestSentenceTransformerProvider:
    def test_raises_if_not_installed(self, monkeypatch):
        """Simulate missing package → ImportError."""
        import sys
        # Force ImportError on sentence_transformers
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        # Make __import__ raise ImportError for sentence_transformers
        import builtins
        original = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sentence_transformers" or name.startswith("sentence_transformers"):
                raise ImportError("simulated missing")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match="sentence-transformers is not installed"):
            SentenceTransformerProvider()

    def test_dim_matches_model(self):
        if not _sentence_transformers_available():
            pytest.skip("sentence-transformers not installed")
        p = SentenceTransformerProvider()
        assert p.dim > 0
        assert p.dim == 384  # all-MiniLM-L6-v2 default

    def test_encode_returns_correct_dim(self):
        if not _sentence_transformers_available():
            pytest.skip("sentence-transformers not installed")
        p = SentenceTransformerProvider()
        v = p.encode("hello world")
        assert len(v) == 384

    def test_encode_semantic_similarity(self):
        """Semantically related phrases should have higher similarity than
        unrelated ones. (Validates the *purpose* of the provider.)"""
        if not _sentence_transformers_available():
            pytest.skip("sentence-transformers not installed")
        p = SentenceTransformerProvider()
        v_concurrency = np.asarray(p.encode("how to handle concurrency"), dtype=np.float64)
        v_async = np.asarray(p.encode("async/await asyncio.gather"), dtype=np.float64)
        v_cooking = np.asarray(p.encode("how to bake bread"), dtype=np.float64)
        sim_related = float(np.dot(v_concurrency, v_async) / (np.linalg.norm(v_concurrency) * np.linalg.norm(v_async)))
        sim_unrelated = float(np.dot(v_concurrency, v_cooking) / (np.linalg.norm(v_concurrency) * np.linalg.norm(v_cooking)))
        assert sim_related > sim_unrelated, (
            f"expected related > unrelated, got {sim_related} vs {sim_unrelated}"
        )


# ── TF-IDF provider ────────────────────────────────────────────────


class TestTfidfProvider:
    def test_dim_property(self):
        p = TfidfEmbeddingProvider(max_features=64)
        assert p.dim == 64

    def test_encode_returns_list(self):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            pytest.skip("scikit-learn not installed")
        p = TfidfEmbeddingProvider(max_features=32)
        v = p.encode("hello world")
        assert isinstance(v, list)
        assert len(v) == 32

    def test_fit_then_encode(self):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            pytest.skip("scikit-learn not installed")
        p = TfidfEmbeddingProvider(max_features=32)
        p.fit(["the cat sat on the mat", "the dog chased the cat"])
        v = p.encode("the cat")
        assert sum(abs(x) for x in v) > 0  # non-zero vector

    def test_encode_pads_to_dim(self):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            pytest.skip("scikit-learn not installed")
        p = TfidfEmbeddingProvider(max_features=64)
        v = p.encode("hi")  # single word, sparse
        assert len(v) == 64


# ── Factory ────────────────────────────────────────────────────────


class TestGetDefaultProvider:
    def test_auto_returns_something(self):
        p = get_default_provider("auto")
        assert p is not None
        assert hasattr(p, "encode")
        assert hasattr(p, "dim")

    def test_hashing_explicit(self):
        p = get_default_provider("hashing", dim=64)
        assert isinstance(p, HashingEmbeddingProvider)
        assert p.dim == 64

    def test_tfidf_explicit(self):
        p = get_default_provider("tfidf", dim=128)
        assert isinstance(p, TfidfEmbeddingProvider)
        assert p.dim == 128

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            get_default_provider("does-not-exist")

    def test_auto_falls_back_to_hashing_if_st_missing(self, monkeypatch):
        """When sentence-transformers is missing, 'auto' should return hashing."""
        if _sentence_transformers_available():
            # Pretend it's missing
            monkeypatch.setattr(
                "agent.core.embeddings._sentence_transformers_available", lambda: False
            )
        p = get_default_provider("auto")
        assert isinstance(p, HashingEmbeddingProvider)

    def test_empty_name_falls_back_to_auto(self):
        p = get_default_provider("")
        assert p is not None
